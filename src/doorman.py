#!/usr/bin/env python3
"""Doorman orchestrator (Phase 2). Listens for door events and runs a policy-aware
Gemini Live voice interaction with tools.

Trigger: subscribes to Frigate MQTT `frigate/events` for the front doorbell camera.
On a `new` person/face event (debounced, not already-interacting), launches a full
voice session: Doorman greets, converses per household policy, and can call
snapshot_front_door / notify_ryan tools.

This reuses the verified audio bridge building blocks from audio_bridge.py:
  - gemini_connect_cm / mic_to_gemini / talkback_connect / SpeakingState
It adds: persona+policies (doorman_prompt), tools (doorman_tools), and an extended
receive loop that dispatches tool_calls.

Run (standalone, auto-triggers on door events):
  python src/doorman.py
Run one interaction immediately (for testing, no MQTT wait):
  python src/doorman.py --once 45 --trigger-text "Test: someone at the door."
"""
import argparse, asyncio, json, logging, sys, os
import audio_bridge as ab
import doorman_prompt
import doorman_tools

log = logging.getLogger("doorman")

# Frigate MQTT config (from the profile .env / frigate.env)
MQTT_HOST = '<ha-host>'
MQTT_PORT = 1883
FRIGATE_TOPIC = 'frigate/events'
FRONT_CAMERA = 'front_doorbell'      # Frigate camera name for the doorbell
INTERACTION_MAX_S = 120               # hard cap on one door interaction
INTERACTION_COOLDOWN_S = 20           # min seconds between interactions


def load_mqtt_creds():
    d = {}
    env = '~/.hermes/profiles/home-admin/.env'
    for line in open(env):
        line = line.strip()
        if '=' in line and not line.startswith('#'):
            k, v = line.split('=', 1)
            v = v.strip().strip('"').strip("'")
            if k in ('MQTT_USER', 'MQTT_PASSWORD'):
                d[k] = v
    return d.get('MQTT_USER'), d.get('MQTT_PASSWORD')


# ---------------------------------------------------------------- extended receive loop w/ tools
async def receive_loop_with_tools(session, audio_out_q, stop_ev, speaking, system_tools=True):
    """Like audio_bridge.gemini_receive_loop but ALSO handles tool_call responses:
    when the model requests snapshot_front_door / notify_ryan, execute and reply."""
    from google.genai import types
    try:
        while not stop_ev.is_set():
            try:
                async for response in session.receive():
                    sc = getattr(response, 'server_content', None)
                    if sc:
                        ot = getattr(sc, 'output_transcription', None)
                        if ot and ot.text:
                            log.info("[gemini said] %s", ot.text)
                        mt = getattr(sc, 'model_turn', None)
                        if mt:
                            for part in (mt.parts or []):
                                if getattr(part, 'inline_data', None) and part.inline_data.data:
                                    data = part.inline_data.data
                                    audio_out_q.put_nowait(bytes(data))
                                    await speaking.mark_active()
                        if getattr(sc, 'turn_complete', False):
                            log.info("turn complete")
                            await speaking.mark_idle()
                    # Handle tool calls
                    tc = getattr(response, 'tool_call', None)
                    if tc and getattr(tc, 'function_calls', None):
                        fns = []
                        for fc in tc.function_calls:
                            log.info("[tool call] %s", fc.name)
                            fns.append(fc)
                        if fns:
                            from google.genai import types as _t
                            results = []
                            for fc in fns:
                                r = await doorman_tools.run_tool_call(fc)
                                results.append(_t.FunctionResponse(
                                    name=fc.name, id=fc.id, response=r))
                            await session.send_tool_response(function_responses=results)
            except asyncio.CancelledError:
                return
            except Exception as e:
                log.warning("gemini receive stream ended: %s", e)
                stop_ev.set()
                return
            await asyncio.sleep(0.2)
    except asyncio.CancelledError:
        pass
    finally:
        await speaking.mark_idle()


# ---------------------------------------------------------------- one interaction
async def run_interaction(system_prompt, trigger_text, duration_s=INTERACTION_MAX_S):
    """Run one full door voice interaction (same proven pipeline as audio_bridge.run_once,
    but with persona + tools). Returns exit code."""
    cfg = ab.load_config()
    audio_q = asyncio.Queue()
    stop_ev = asyncio.Event()
    cfg_tools = {'response_modalities': ['AUDIO'],
                 'system_instruction': {'parts': [{'text': system_prompt}]},
                 'tools': [{'function_declarations': doorman_tools.tool_declarations()}]}

    from google import genai
    client = genai.Client(api_key=cfg['GEMINI_API_KEY'])
    model = 'gemini-3.1-flash-live-preview'
    cm = client.aio.live.connect(model=model, config=cfg_tools)

    async with cm as session:
        try:
            pc, ws, mic, keep_task = await ab.talkback_connect(cfg, audio_q)
        except Exception as e:
            log.error("talkback connect failed: %s", e)
            return 2

        speaking = ab.SpeakingState()
        recv_task = asyncio.create_task(
            receive_loop_with_tools(session, audio_q, stop_ev, speaking))
        mic_task = asyncio.create_task(ab.mic_to_gemini(session, stop_ev, speaking))
        log.info("interaction starting (max %ss): %s", duration_s, trigger_text)
        try:
            await asyncio.sleep(1.0)
            await session.send_realtime_input(text=trigger_text)
        except Exception as e:
            log.warning("prime err: %s", e)

        try:
            await asyncio.wait_for(stop_ev.wait(), timeout=duration_s)
        except asyncio.TimeoutError:
            log.info("interaction duration elapsed")
        log.info("ending interaction")
        recv_task.cancel(); mic_task.cancel(); keep_task.cancel()
        try:
            await pc.close()
        except Exception:
            pass
        try:
            await ws.close()
        except Exception:
            pass
    return 0


# ---------------------------------------------------------------- MQTT listener
async def frigate_event_listener(handle_event):
    """Subscribe to frigate/events; call handle_event(event_dict) for relevant new events.
    Yields relevant triggers. Blocks forever (or until cancelled)."""
    import paho.mqtt.client as mqtt
    loop = asyncio.get_event_loop()
    events = asyncio.Queue()
    user, pw = load_mqtt_creds()

    def on_message(client, userdata, msg):
        try:
            payload = json.loads(msg.payload.decode())
            # forward a parsed relevant event; let the caller decide
            asyncio.run_coroutine_threadsafe(events.put(payload), loop)
        except Exception:
            pass

    client = mqtt.Client()
    client.username_pw_set(user, pw)
    client.on_message = on_message
    client.connect(MQTT_HOST, MQTT_PORT, 60)
    client.subscribe(FRIGATE_TOPIC)
    log.info("subscribed to %s", FRIGATE_TOPIC)
    # run mqtt in background thread
    import threading
    def run():
        client.loop_forever()
    threading.Thread(target=run, daemon=True).start()

    last_trigger_ts = 0.0
    import time
    while True:
        ev = await events.get()
        # parse
        after = ev.get('after', {})
        camera = after.get('camera', '')
        label = after.get('label', '')
        etype = ev.get('type', '')
        if camera != FRONT_CAMERA:
            continue
        # relevant: person/cat/dog/face new event, or doorbell-specific
        # We trigger on 'new' events for people-ish labels, plus sub_label for faces
        if etype != 'new':
            continue
        if label not in ('person', 'cat', 'dog', 'face'):
            continue
        sub_label = after.get('sub_label')
        # debounce + not already interacting handled by caller
        now = time.monotonic()
        if now - last_trigger_ts < INTERACTION_COOLDOWN_S:
            log.info("event debounced (cooldown)")
            continue
        last_trigger_ts = now
        # Build trigger context
        recognized = sub_label if sub_label else None
        doorbell_pressed = False  # Frigate doesn't know the physical ring; see note
        trigger_text = doorman_prompt.interaction_trigger_text(
            recognized_name=recognized, doorbell_pressed=doorbell_pressed, label=label)
        prompt = doorman_prompt.build_doorman_prompt(recognized_name=recognized)
        await handle_event(prompt, trigger_text, {'label': label, 'sub_label': sub_label})


# ---------------------------------------------------------------- main / CLI
async def amain(args):
    if args.once:
        # Run a single interaction immediately (testing), no MQTT wait.
        prompt = doorman_prompt.build_doorman_prompt(
            recognized_name=getattr(args, 'recognized', None))
        trigger = args.trigger_text or doorman_prompt.interaction_trigger_text()
        return await run_interaction(prompt, trigger, duration_s=args.once)

    # Full service: listen for door events.
    busy = asyncio.Event()  # not used to block, but to note a running interaction
    async def handle_event(prompt, trigger_text, meta):
        log.info("TRIGGER: %s", trigger_text)
        # launch interaction; serialize so we don't overlap
        try:
            await asyncio.wait_for(
                run_interaction(prompt, trigger_text, duration_s=INTERACTION_MAX_S),
                timeout=INTERACTION_MAX_S + 15)
        except asyncio.TimeoutError:
            log.warning("interaction overran cap")
        log.info("interaction done")

    await frigate_event_listener(handle_event)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--once', type=float, default=0,
                    help='if >0, run one interaction for N seconds immediately and exit')
    ap.add_argument('--trigger-text', default='',
                    help='custom trigger text for --once (else a generic greeting)')
    ap.add_argument('--recognized', default=None,
                    help='recognized name for --once (else treated as unknown)')
    ap.add_argument('-v', '--verbose', action='store_true')
    a = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format='%(asctime)s %(levelname)s %(message)s')
    code = asyncio.run(amain(a))
    sys.exit(code)


if __name__ == '__main__':
    main()
