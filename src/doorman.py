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

# Frigate MQTT config (from doorman_config: env > profile files > defaults)
import doorman_config as _dc
_CFG = _dc.load()
MQTT_HOST = _CFG['MQTT_HOST']
MQTT_PORT = _CFG['MQTT_PORT']
FRIGATE_TOPIC = _CFG['FRIGATE_TOPIC']
FRONT_CAMERA = _CFG['FRONT_CAMERA']      # Frigate camera name for the doorbell
ANIMAL_LABELS = ('cat', 'dog')  # Frigate labels that get the animal reaction
                                # (short animal-aware greeting, not a full human convo)
ANIMAL_MAX_S = _CFG['DOORMAN_ANIMAL_MAX_S']  # hard cap on one animal voice session
INTERACTION_MAX_S = _CFG['INTERACTION_MAX_S']               # hard cap on one door interaction
INTERACTION_COOLDOWN_S = _CFG['INTERACTION_COOLDOWN_S']     # min seconds between interactions
IDLE_TIMEOUT_S = _CFG['IDLE_TIMEOUT_S']                     # end interaction after this many idle seconds


def load_mqtt_creds():
    """Return (MQTT_USER, MQTT_PASSWORD) from the merged config."""
    cfg = _dc.load()
    return cfg.get('MQTT_USER'), cfg.get('MQTT_PASSWORD')


# ---------------------------------------------------------------- extended receive loop w/ tools
async def receive_loop_with_tools(session, audio_out_q, stop_ev, speaking, activity=None):
    """Like audio_bridge.gemini_receive_loop but ALSO handles tool_call responses:
    when the model requests snapshot_front_door / notify_ryan, execute and reply.
    If `activity` (ActivityClock) is given, it is marked on visitor/AI/tool activity
    so an idle watchdog can end the interaction when the visitor goes silent."""
    from google.genai import types
    try:
        while not stop_ev.is_set():
            try:
                async for response in session.receive():
                    sc = getattr(response, 'server_content', None)
                    if sc:
                        # visitor speech heard (resets idle)
                        it_ = getattr(sc, 'input_transcription', None)
                        if it_ and getattr(it_, 'text', None):
                            log.info("[visitor said] %s", it_.text)
                            if activity:
                                await activity.mark()
                        iit_ = getattr(sc, 'interim_input_transcription', None)
                        if iit_ and getattr(iit_, 'text', None) and activity:
                            await activity.mark()
                        ot = getattr(sc, 'output_transcription', None)
                        if ot and ot.text:
                            log.info("[gemini said] %s", ot.text)
                            if activity:
                                await activity.mark()
                        mt = getattr(sc, 'model_turn', None)
                        if mt:
                            for part in (mt.parts or []):
                                if getattr(part, 'inline_data', None) and part.inline_data.data:
                                    data = part.inline_data.data
                                    audio_out_q.put_nowait(bytes(data))
                                    await speaking.mark_active()
                                    if activity:
                                        await activity.mark()
                        if getattr(sc, 'turn_complete', False):
                            log.info("turn complete")
                            await speaking.mark_idle()
                    # Handle tool calls
                    tc = getattr(response, 'tool_call', None)
                    if tc and getattr(tc, 'function_calls', None):
                        fns = []
                        for fc in tc.function_calls:
                            log.info("[tool call] %s", fc.name)
                            if activity:
                                await activity.mark()
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
class ActivityClock:
    """Tracks the last time there was meaningful activity (visitor speech, AI speech,
    or a tool call) so the interaction can end after an idle period. Used to stop
    listening / wrap up when the visitor goes silent."""
    def __init__(self):
        import time
        self._last = time.monotonic()
        self._lock = asyncio.Lock()

    async def mark(self):
        import time
        async with self._lock:
            self._last = time.monotonic()

    async def idle_seconds(self):
        import time
        async with self._lock:
            return time.monotonic() - self._last


async def run_interaction(system_prompt, trigger_text, duration_s=INTERACTION_MAX_S,
                          idle_timeout_s=None):
    """Run one full door voice interaction (same proven pipeline as audio_bridge.run_once,
    but with persona + tools). Returns exit code.

    idle_timeout_s: if set, end the interaction after this many seconds with no visitor
    speech / AI speech / tool activity (defaults to INTERACTION_MAX_S, i.e. no early cut).
    """
    activity = ActivityClock()  # marks visitor/AI/tool activity; idle watchdog reads it
    cfg = ab.load_config()
    audio_q = asyncio.Queue()
    stop_ev = asyncio.Event()
    cfg_tools = {'response_modalities': ['AUDIO'],
                 'system_instruction': {'parts': [{'text': system_prompt}]},
                 'tools': [{'function_declarations': doorman_tools.tool_declarations()}]}
    cfg_tools.update(ab.voice_speech_config(cfg))

    from google import genai
    client = genai.Client(api_key=cfg['GEMINI_API_KEY'])
    model = 'gemini-3.1-flash-live-preview'
    cm = client.aio.live.connect(model=model, config=cfg_tools)

    async with cm as session:
        # Talkback connect with retry. Opening the AD410's backchannel session
        # races the camera's two always-on streams: even with record+detect
        # consolidated onto go2rtc relays, the FIRST backchannel open can hit a
        # one-shot RTSP i/o timeout while the camera's audio subsystem settles.
        # The backchannel streams fine immediately afterward (verified by direct
        # pull), so retry with a settle delay before giving up on the greeting.
        pc = ws = mic = keep_task = recv_holder = None
        for attempt in range(3):
            try:
                pc, ws, mic, keep_task, recv_holder = await ab.talkback_connect(cfg, audio_q)
                break
            except Exception as e:
                log.warning("talkback connect attempt %d/3 failed: %s", attempt + 1,
                            (str(e).splitlines()[0] if str(e) else e)[:120])
                if attempt < 2:
                    await asyncio.sleep(8.0)
        if ws is None:
            log.error("talkback connect failed after 3 attempts; skipping interaction")
            return 2

        speaking = ab.SpeakingState()
        recv_task = asyncio.create_task(
            receive_loop_with_tools(session, audio_q, stop_ev, speaking, activity))
        # MIC SOURCE selection. Default is the go2rtc RELAY, not the twoway
        # connection's received-audio track.
        #
        # History (2026-09-10): this used to prefer mic_from_webtrack whenever
        # recv_holder had a track. go2rtc ALWAYS offers that track (sendonly
        # PCMA/8000), so the webtrack path was always taken - and it was never
        # observed to carry the visitor's voice. Three rings produced greeting-only
        # conversations: Gemini transcribed nothing and every interaction sat out its
        # 25s idle timeout. The track's presence silently shadowed the working relay
        # path. The relay is therefore the default, and the webtrack is opt-in
        # (DOORMAN_MIC_SOURCE=webtrack) for A/B testing.
        mic_source = str(cfg.get('DOORMAN_MIC_SOURCE', 'relay') or 'relay').strip().lower()
        if mic_source == 'webtrack' and recv_holder.get('track') is not None:
            log.info("mic source: webtrack (twoway received-audio track)")
            mic_task = asyncio.create_task(
                ab.mic_from_webtrack(session, recv_holder, stop_ev, speaking))
        else:
            if mic_source == 'webtrack':
                log.warning("mic source: webtrack requested but no received-audio "
                            "track offered; using relay instead")
            log.info("mic source: relay %s",
                     str(cfg.get('DOORMAN_MIC_RTSP') or '').split('@')[-1])
            mic_task = asyncio.create_task(ab.mic_to_gemini(session, stop_ev, speaking))
        log.info("interaction starting (max %ss%s): %s", duration_s,
                 f", idle-stop {idle_timeout_s}s" if idle_timeout_s else "", trigger_text)
        try:
            await asyncio.sleep(1.0)
            await session.send_realtime_input(text=trigger_text)
        except Exception as e:
            log.warning("prime err: %s", e)

        # Watch for either the hard cap or (if configured) an idle period with no activity.
        import time
        interaction_start = time.monotonic()
        try:
            if idle_timeout_s:
                while not stop_ev.is_set():
                    idle = await activity.idle_seconds()
                    if idle >= idle_timeout_s:
                        log.info("idle for %.0fs >= %ss, ending interaction", idle, idle_timeout_s)
                        break
                    if (time.monotonic() - interaction_start) >= duration_s:
                        log.info("interaction duration elapsed")
                        break
                    await asyncio.sleep(0.5)
            else:
                await asyncio.wait_for(stop_ev.wait(), timeout=duration_s)
        except asyncio.TimeoutError:
            log.info("interaction duration elapsed")
        log.info("ending interaction")
        # Graceful teardown of the talkback WebRTC connection. Abrupt close of the
        # go2rtc consumer crashes the AD410's two-way backchannel (observed: crash on
        # session end after a working conversation). Order matters:
        #   1. stop producing audio (recv + mic tasks)
        #   2. let the last queued AI audio flush to the speaker
        #   3. close the peer connection (sends RTCP BYE) while the signaling WS is
        #      still being read so go2rtc processes the close
        #   4. settle briefly so the camera releases the backchannel
        #   5. then shut the signaling WS down
        recv_task.cancel(); mic_task.cancel()
        try:
            await asyncio.wait_for(asyncio.gather(recv_task, mic_task, return_exceptions=True), timeout=2)
        except Exception:
            pass
        # let queued AI audio flush to the speaker so we don't cut off mid-word
        try:
            end = asyncio.get_event_loop().time() + 0.3
            while asyncio.get_event_loop().time() < end:
                await asyncio.sleep(0.02)
        except Exception:
            pass
        # close the peer connection (graceful RTCP BYE) while WS still being read
        try:
            await pc.close()
        except Exception:
            pass
        # brief pause so go2rtc/camera releases the backchannel cleanly
        try:
            await asyncio.sleep(0.8)
        except Exception:
            pass
        keep_task.cancel()
        try:
            await asyncio.wait_for(asyncio.gather(keep_task, return_exceptions=True), timeout=1)
        except Exception:
            pass
        try:
            await ws.close()
        except Exception:
            pass
    return 0


# ---------------------------------------------------------------- MQTT listener
def _parse_sub_label(sub):
    """Frigate sub_label can be None, a str name, or a list ['Name', confidence].
    Return the clean name string or None."""
    if not sub:
        return None
    if isinstance(sub, (list, tuple)):
        name = sub[0] if sub else None
        return name if isinstance(name, str) else None
    return sub if isinstance(sub, str) else None


def _decide_trigger_action(label, animal_behavior):
    """Pick the Doorman reaction for a Frigate label.

    Returns one of:
      'animal-voice'   label in ANIMAL_LABELS, behavior is voice (or unset) - default
      'animal-notify'  label in ANIMAL_LABELS, behavior == 'notify'
      'animal-off'     label in ANIMAL_LABELS, behavior == 'off' (ignore)
      'person'         not an animal (normal human-conversation voice path)
    """
    if label in ANIMAL_LABELS:
        b = str(animal_behavior or 'voice').strip().lower()
        if b == 'off':
            return 'animal-off'
        if b == 'notify':
            return 'animal-notify'
        return 'animal-voice'
    return 'person'


async def frigate_event_listener(handle_event, personalized_greeting=True):
    """Subscribe to frigate/events; trigger Doorman when a person is at the door.

    personalized_greeting=True (default): WAIT for face recognition before deciding
    whether to greet as known or unknown (greets by name if recognized; adds ~10-20s
    before first speech).

    personalized_greeting=False: trigger IMMEDIATELY on the first person detection with
    a generic greeting (no recognition wait, no by-name). Fast first response.

    Frigate event lifecycle for a recognized person (verified 2026-09-08):
      new    sub=None                (person first detected)
      update sub=None                (still tracking)
      update sub=['Ryan', 0.96]      (recognition result arrives, ~10-20s later)
      update sub=['Ryan', 0.95]
      end    sub=['Ryan', 0.95]
    """
    import paho.mqtt.client as mqtt
    loop = asyncio.get_event_loop()
    events = asyncio.Queue()
    user, pw = load_mqtt_creds()

    def on_message(client, userdata, msg):
        try:
            payload = json.loads(msg.payload.decode())
            asyncio.run_coroutine_threadsafe(events.put(payload), loop)
        except Exception:
            pass

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    client.username_pw_set(user, pw)
    client.on_message = on_message
    client.connect(MQTT_HOST, MQTT_PORT, 60)
    client.subscribe(FRIGATE_TOPIC)
    log.info("subscribed to %s", FRIGATE_TOPIC)
    import threading
    def run():
        client.loop_forever()
    threading.Thread(target=run, daemon=True).start()

    import time
    # state: event_id -> {'new_ts': monotonic, 'recognized': name-or-None, 'triggered': bool}
    pending = {}
    RECOGNIZE_GRACE_S = 25.0   # how long to wait for recognition after 'new'
    last_trigger_ts = 0.0

    while True:
        ev = await events.get()
        after = ev.get('after', {})
        camera = after.get('camera', '')
        label = after.get('label', '')
        etype = ev.get('type', '')
        event_id = after.get('id', '')
        if camera != FRONT_CAMERA:
            continue
        if label not in ('person', 'cat', 'dog', 'face'):
            continue
        now = time.monotonic()
        name = _parse_sub_label(after.get('sub_label'))

        # ---- Fast path: personalized greeting OFF -> greet immediately on detection ----
        if not personalized_greeting:
            if etype == 'new' and label in ('person', 'cat', 'dog', 'face'):
                if now - last_trigger_ts < INTERACTION_COOLDOWN_S:
                    log.info("trigger debounced (cooldown)")
                    continue
                last_trigger_ts = now
                log.info("TRIGGER (no recognition wait): %s at the door", label)
                trigger_text = doorman_prompt.interaction_trigger_text(
                    recognized_name=None, doorbell_pressed=False, label=label)
                prompt = doorman_prompt.build_doorman_prompt(recognized_name=None)
                await handle_event(prompt, trigger_text, {'label': label, 'name': None})
            # expire nothing; simple path
            expired = [eid for eid, p in pending.items()
                       if (now - p.get('new_ts', 0)) > 60]
            for eid in expired:
                del pending[eid]
            continue

        if etype == 'new':
            # start a pending track for this visitor
            if event_id and event_id not in pending:
                pending[event_id] = {'new_ts': now, 'name': name}
                log.info("person seen (event %s), waiting for recognition...", event_id[:12])
        elif etype == 'update':
            if event_id in pending:
                # recognition arrived?
                if name:
                    pending[event_id]['name'] = name
                    log.info("recognized %s for event %s", name, event_id[:12])
                # trigger once recognition is known (may be immediate or after several updates)
        elif etype == 'end':
            if event_id in pending:
                if name and pending[event_id]['name'] is None:
                    pending[event_id]['name'] = name
                log.info("event %s ended (name=%s)", event_id[:12], pending[event_id]['name'])

        # ---- trigger decision for a settled event (fire exactly once per event id) ----
        if event_id in pending:
            p = pending[event_id]
            elapsed = now - p['new_ts']
            settled = (p['name'] is not None) or (etype == 'end') or (elapsed >= RECOGNIZE_GRACE_S)
            if settled and not p.get('triggered'):
                p['triggered'] = True
                # fire unless debounced by cooldown
                if now - last_trigger_ts < INTERACTION_COOLDOWN_S:
                    log.info("trigger debounced (cooldown); recognized=%s", p['name'])
                else:
                    last_trigger_ts = now
                    p['fired'] = True
                    recognized = p['name']
                    log.info("TRIGGER: %s at the door (recognized=%s)", label, recognized)
                    trigger_text = doorman_prompt.interaction_trigger_text(
                        recognized_name=recognized, doorbell_pressed=False, label=label)
                    prompt = doorman_prompt.build_doorman_prompt(recognized_name=recognized)
                    await handle_event(prompt, trigger_text, {'label': label, 'name': recognized})

        # expire stale pending entries
        expired = [eid for eid, p in pending.items()
                   if p.get('triggered', False) and (now - p['new_ts']) > 60]
        for eid in expired:
            del pending[eid]


# ---------------------------------------------------------------- doorbell-press trigger (HA WebSocket)
async def doorbell_event_listener(handle_event, sensor='binary_sensor.doorbell_pressed'):
    """Subscribe to HA state_changed events and trigger Doorman when the doorbell
    sensor transitions to 'on'.

    Event-driven via HA WebSocket (no polling -> no HA IP-ban risk). On a press
    (new_state == 'on'), launches a voice interaction with doorbell_pressed=True so
    the model greets someone who rang. Debounced by INTERACTION_COOLDOWN_S.
    """
    import asyncio as _aio
    import websockets
    import json as _json
    cfg = _dc.load()
    hass_ws = (cfg.get('HASS_URL') or 'http://<ha-host>:8123').replace('http://', 'ws://').replace('https://', 'wss://') + '/api/websocket'
    token = cfg.get('HASS_TOKEN') or ''
    if not token:
        raise RuntimeError("doorbell trigger mode requires HASS_TOKEN (HA long-lived token)")

    last_trigger_ts = 0.0
    import time as _time
    # The HA WebSocket is long-lived and can drop mid-interaction (default keepalive
    # ping interval is shorter than a voice session -> "keepalive ping timeout").
    # That used to be fatal: ConnectionClosedError propagated out of
    # doorbell_event_listener -> asyncio.gather -> amain -> process exit -> container
    # restart (RestartCount incremented every interaction). Now: longer keepalive
    # timeouts AND a reconnect loop so a drop logs + re-subscribes instead of dying.
    while True:
        try:
            async with websockets.connect(hass_ws, max_size=10 * 1024 * 1024, open_timeout=30,
                                          ping_interval=30, ping_timeout=60) as ws:
                # HA auth handshake
                msg = _json.loads(await ws.recv())
                if msg.get('type') != 'auth_required':
                    raise RuntimeError("HA websocket did not ask for auth: %s" % msg)
                await ws.send(_json.dumps({'type': 'auth', 'access_token': token}))
                auth = _json.loads(await ws.recv())
                if auth.get('type') != 'auth_ok':
                    raise RuntimeError("HA websocket auth failed: %s" % auth)
                # subscribe to state_changed
                await ws.send(_json.dumps({'id': 1, 'type': 'subscribe_events', 'event_type': 'state_changed'}))
                sub = _json.loads(await ws.recv())
                if not sub.get('success'):
                    raise RuntimeError("subscribe_events failed: %s" % sub)
                log.info("doorbell listener subscribed to HA state_changed (sensor=%s)", sensor)

                while True:
                    raw = await ws.recv()
                    try:
                        msg = _json.loads(raw)
                    except Exception:
                        continue
                    ev_type = msg.get('type')
                    if ev_type == 'event':
                        ev = msg.get('event', {})
                        data = ev.get('data', {})
                        if data.get('entity_id') != sensor:
                            continue
                        new_state = (data.get('new_state') or {})
                        old_state = (data.get('old_state') or {})
                        n = new_state.get('state')
                        o = old_state.get('state')
                        now = _time.monotonic()
                        if n == 'on' and o != 'on' and (now - last_trigger_ts) >= INTERACTION_COOLDOWN_S:
                            last_trigger_ts = now
                            log.info("TRIGGER (doorbell press): %s -> %s", o, n)
                            trigger_text = doorman_prompt.interaction_trigger_text(
                                recognized_name=None, doorbell_pressed=True, label='person')
                            prompt = doorman_prompt.build_doorman_prompt(recognized_name=None)
                            await handle_event(prompt, trigger_text, {'label': 'person', 'name': None, 'doorbell_pressed': True})
                    elif ev_type == 'result' and msg.get('id') == 1:
                        pass  # subscription confirmed
                    elif ev_type == 'ping':
                        await ws.send(_json.dumps({'type': 'pong', 'id': msg.get('id')}))
        except _aio.CancelledError:
            raise
        except websockets.ConnectionClosed as e:
            log.warning("HA WS doorbell listener dropped (%s); reconnecting in 3s", e)
            await _aio.sleep(3)
        except Exception as e:
            log.error("HA WS doorbell listener error (%s); reconnecting in 5s", e)
            await _aio.sleep(5)


# ---------------------------------------------------------------- main / CLI
async def amain(args):
    if args.once:
        # Run a single interaction immediately (testing), no MQTT wait.
        prompt = doorman_prompt.build_doorman_prompt(
            recognized_name=getattr(args, 'recognized', None))
        trigger = args.trigger_text or doorman_prompt.interaction_trigger_text()
        return await run_interaction(prompt, trigger, duration_s=args.once,
                                     idle_timeout_s=IDLE_TIMEOUT_S)

    # Full service: listen for door events.
    # Personalized greeting config: false -> greet immediately (no recognition wait).
    cfg = ab.load_config()
    personalized = bool(cfg.get('DOORMAN_PERSONALIZED_GREETING', True))
    trigger_mode = str(cfg.get('DOORMAN_TRIGGER_MODE', 'person')).strip().lower()
    doorbell_sensor = cfg.get('DOORMAN_DOORBELL_SENSOR') or 'binary_sensor.doorbell_pressed'
    log.info("personalized greeting enabled: %s", personalized)
    log.info("trigger mode: %s", trigger_mode)
    busy = asyncio.Event()  # not used to block, but to note a running interaction
    async def handle_event(prompt, trigger_text, meta):
        log.info("TRIGGER: %s", trigger_text)
        # Camera health gate. The AD410 wedges its RTSP server around interactions
        # (self-recovers in ~1-5 min) and both cheap proxies lie about it: HA's
        # camera_status sensor polls HTTP :80 and reported `up` through 40s of total
        # RTSP failure, and go2rtc keeps a stale producer record with a populated
        # remote_addr after the camera stops answering. The gate therefore keys off
        # Frigate's actual frame flow (camera_fps/process_fps), the only signal
        # observed to track an outage. Bounded so a ring is never dropped forever.
        try:
            ok = await ab.wait_camera_healthy(max_wait_s=90, poll_s=5)
            if ok:
                log.info("camera health gate: camera ready")
            else:
                log.warning("camera health gate: timed out; starting anyway")
        except Exception as e:
            log.warning("camera health gate error: %s; proceeding", e)
        # launch interaction; serialize so we don't overlap. End early if the visitor
        # goes silent (idle) so Doorman stops listening and can re-trigger later.
        try:
            await asyncio.wait_for(
                run_interaction(prompt, trigger_text, duration_s=INTERACTION_MAX_S,
                                idle_timeout_s=IDLE_TIMEOUT_S),
                timeout=INTERACTION_MAX_S + 15)
        except asyncio.TimeoutError:
            log.warning("interaction overran cap")
        log.info("interaction done")

    tasks = []
    if trigger_mode in ('doorbell', 'hybrid'):
        tasks.append(doorbell_event_listener(handle_event, sensor=doorbell_sensor))
    if trigger_mode in ('person', 'hybrid'):
        tasks.append(frigate_event_listener(handle_event, personalized_greeting=personalized))

    # one or both listeners run concurrently, feeding the same handle_event
    await asyncio.gather(*tasks)


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
