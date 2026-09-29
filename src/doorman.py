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
# Master on/off for animal reactions, independent of DOORMAN_TRIGGER_MODE. When
# True, the Frigate listener runs in ANY mode so cat/dog events are seen and each
# triggers the reaction per DOORMAN_ANIMAL_BEHAVIOR; when False, animal events are
# ignored regardless of mode (and the Frigate listener only runs if person
# triggering needs it).
ANIMAL_REACTIONS = _CFG['DOORMAN_ANIMAL_REACTIONS']
IGNORED_FACES = set()  # overridden by amain() from config; recognized names here are fully ignored
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
    if str(_CFG.get('DOORMAN_VOICE_ENGINE', 'gemini')).strip().lower() == 'local':
        log.info("voice engine: local")
        import voice_local as _vl
        return await _vl.run_interaction_local(system_prompt, trigger_text,
                                               duration_s=duration_s,
                                               idle_timeout_s=idle_timeout_s)
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
        # RING-SETTLE WAIT: never open the backchannel inside the AD410's
        # ~7-8s post-press "blue" window (verified 2026-09-25: opening bc=1
        # during that window wedges the camera's RTSP server; after it is safe).
        # If a press was tracked within the window, wait out the remainder.
        try:
            await ab.ring_settle_wait(cfg, log=log)
        except Exception as e:
            log.warning("ring-settle wait error: %s", str(e)[:80])
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
        except asyncio.CancelledError:
            # Outer hard cap (INTERACTION_MAX_S + 15) cancels this task before the
            # watchdog's own duration; teardown must still run (finally) or the
            # talkback WebRTC + go2rtc signaling leak and the camera stays in
            # two-way mode.
            log.info("interaction cancelled by outer cap; tearing down talkback")
            raise
        finally:
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
class DoorZoneGate:
    """Supplemental gate: an HA occupancy sensor that must have been 'on'
    continuously for hold_s seconds before a person/face Frigate trigger may fire.
    mark_on/mark_off are fed by HA state_changed events; satisfied(now) is the
    query the listener asks at trigger time. 'off' or 'unavailable' reset the hold
    (treat both as not-on)."""
    def __init__(self, entity_id, hold_s):
        self.entity_id = entity_id
        self.hold_s = float(hold_s)
        self._on_since = None

    def mark_on(self, now):
        if self._on_since is None:
            self._on_since = now

    def mark_off(self, now):
        self._on_since = None

    def in_seconds(self, now):
        return 0.0 if self._on_since is None else now - self._on_since

    def satisfied(self, now):
        return self.in_seconds(now) >= self.hold_s


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


async def animal_reaction(label, cfg, behavior):
    """Animal reaction: notify Ryan (attaches a fresh doorbell frame), and for
    'voice' run a short, animal-aware voice session. The caller has already
    passed the camera-health gate before invoking this."""
    import doorman_tools as _tools
    msg = "A %s is at the front door." % label
    try:
        ok, val = await _tools.notify_ryan(msg)
        log.info("animal notify for %s -> %s", label, val)
    except Exception as e:
        log.warning("animal notify error: %s", e)
    if str(behavior).lower() != 'voice':
        return
    aprompt = doorman_prompt.build_doorman_prompt(animal_label=label)
    atrigger = doorman_prompt.interaction_trigger_text(label=label, animal=True)
    log.info("animal voice session starting for %s (cap %ss)", label, ANIMAL_MAX_S)
    try:
        await asyncio.wait_for(
            run_interaction(aprompt, atrigger, duration_s=ANIMAL_MAX_S,
                            idle_timeout_s=IDLE_TIMEOUT_S),
            timeout=ANIMAL_MAX_S + 15)
    except asyncio.TimeoutError:
        log.warning("animal interaction overran cap")


def _label_allowed(label, person_trigger, animals_trigger):
    """Whether the Frigate listener should act on `label` given which trigger
    classes are enabled. cat/dog need animals_trigger; person/face need
    person_trigger. Unknown labels are ignored."""
    if label in ANIMAL_LABELS:
        return bool(animals_trigger)
    if label in ('person', 'face'):
        return bool(person_trigger)
    return False


async def frigate_event_listener(handle_event, personalized_greeting=True, gate=None,
                                 person_trigger=True, animals_trigger=True):
    """Subscribe to frigate/events; trigger Doorman when a person is at the door.

    person_trigger / animals_trigger: which label classes this listener acts on.
    person_trigger gates person/face events; animals_trigger gates cat/dog events.
    This lets animal reactions be enabled in ANY trigger mode (the listener runs
    with person_trigger=False in doorbell mode when animals are on) and lets
    animals be disabled without changing the trigger mode.

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

    def on_connect(client, userdata, flags, reason_code, properties=None):
        # Fresh connect (rc=0). MUST re-subscribe here: Paho auto-reconnects the
        # TCP socket after a network blip (e.g. 2026-09-18 10:01 HA-box outage
        # dropped the broker link silently), but with a clean session the broker
        # forgets the subscription and Paho does NOT re-subscribe on its own.
        # Without this, every Frigate event after any blip is delivered to no one
        # and the doorman goes deaf (observed: 4 person events 08:02-08:08 UTC,
        # zero 'person seen' log lines).
        if reason_code == 0:
            client.subscribe(FRIGATE_TOPIC)
            log.info("mqtt connected, (re)subscribed to %s", FRIGATE_TOPIC)

    def on_disconnect(client, userdata, flags, reason_code, properties=None):
        log.warning("mqtt disconnected (rc=%s); auto-reconnect will re-subscribe", reason_code)

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    client.username_pw_set(user, pw)
    client.on_message = on_message
    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    client.reconnect_delay_set(min_delay=1, max_delay=30)
    client.connect(MQTT_HOST, MQTT_PORT, 60)
    # subscription happens in on_connect (fires on first CONNACK inside loop_forever)
    import threading
    def run():
        client.loop_forever()
    threading.Thread(target=run, daemon=True).start()

    import time
    # state: event_id -> {'new_ts': monotonic, 'recognized': name-or-None, 'triggered': bool}
    pending = {}
    # How long to wait for Frigate face recognition after a person is detected,
    # before triggering as an unknown visitor. Configurable via
    # DOORMAN_RECOGNIZE_GRACE_S (default 12 s; was hardcoded 25 s).
    RECOGNIZE_GRACE_S = _dc.load().get('DOORMAN_RECOGNIZE_GRACE_S', 12.0)
    last_trigger_ts = 0.0
    fast_fired = set()   # fast-path event_ids already triggered (person/face hold re-checks on updates)

    def gate_blocked(label, now):
        """True if a gate is configured, applies to this label, and is not yet held."""
        if gate is None or label not in ('person', 'face'):
            return False
        if gate.satisfied(now):
            return False
        log.info("door-zone gate: %.1fs/%.1fs held, not triggering (label=%s)",
                 gate.in_seconds(now), gate.hold_s, label)
        return True

    async def _decide(event_id, now, etype=None):
        """Fire the trigger for a pending event once it's settled.

        Settled = recognized (name known), event ended, OR the recognition grace
        has elapsed. Called from both the Frigate-event path and the periodic
        ticker, so the trigger fires at grace_ms even when Frigate stops sending
        updates for a stationary person (the 2026-09-17 '78 s greeting' bug:
        the trigger used to wait for Frigate's next event, which for a stationary
        person could be a minute or more away).
        """
        nonlocal last_trigger_ts
        p = pending.get(event_id)
        if p is None or p.get('triggered'):
            return
        label = p.get('label', 'person')
        elapsed = now - p['new_ts']
        settled = (p['name'] is not None) or (etype == 'end') \
            or (elapsed >= RECOGNIZE_GRACE_S) \
            or (label in ANIMAL_LABELS)
        if not settled:
            return
        if gate_blocked(label, now):
            # not held long enough on the gate; a later update/end/tick can fire
            return
        # Ignored faces: a recognized name on the ignore list gets NO greeting.
        if p['name'] and p['name'].strip().lower() in IGNORED_FACES:
            log.info("ignored face %s at the door; no greeting", p['name'])
            p['triggered'] = True
            del pending[event_id]
            return
        p['triggered'] = True
        if now - last_trigger_ts < INTERACTION_COOLDOWN_S:
            log.info("trigger debounced (cooldown); recognized=%s", p['name'])
            return
        last_trigger_ts = now
        p['fired'] = True
        recognized = p['name']
        log.info("TRIGGER: %s at the door (recognized=%s)", label, recognized)
        trigger_text = doorman_prompt.interaction_trigger_text(
            recognized_name=recognized, doorbell_pressed=False, label=label)
        prompt = doorman_prompt.build_doorman_prompt(recognized_name=recognized)
        await handle_event(prompt, trigger_text, {'label': label, 'name': recognized})

    # Ticker so the trigger decision re-checks even when Frigate goes quiet.
    # Frigate only pushes events when a tracked object changes; for a stationary
    # person that can be a minute or more. Previously the trigger only ran on a
    # Frigate event, so a settled event (recognition grace elapsed) sat until
    # Frigate's next update/end -- the 2026-09-17 '78 s greeting' bug. Now a
    # background task drops a tick sentinel into the same queue every TICK_S and
    # the loop re-evaluates pending events on it. No Frigate event is dropped.
    TICK_S = 5.0

    async def _ticker():
        while True:
            await asyncio.sleep(TICK_S)
            await events.put({'tick': True})

    asyncio.ensure_future(_ticker())

    while True:
        ev = await events.get()
        if not isinstance(ev, dict):
            # malformed payload (non-dict); skip, never crash on one bad message
            continue
        if ev.get('tick'):
            now = time.monotonic()
            for eid in list(pending.keys()):
                await _decide(eid, now)
            # expire stale pending entries
            expired = [eid for eid, p in pending.items()
                       if p.get('triggered', False) and (now - p['new_ts']) > 60]
            for eid in expired:
                del pending[eid]
            continue
        after = ev.get('after') or ev  # tolerate null/absent 'after' (fields at top level)
        if not isinstance(after, dict):
            log.warning("frigate event: non-dict payload skipped: %r", str(ev)[:120])
            continue
        camera = after.get('camera', '')
        label = after.get('label', '')
        etype = ev.get('type', '')
        event_id = after.get('id', '')
        if camera != FRONT_CAMERA:
            continue
        if label not in ('person', 'cat', 'dog', 'face'):
            continue
        # Gate by label class so person-triggering and animal-triggering can be
        # enabled/disabled independently (see person_trigger / animals_trigger).
        if not _label_allowed(label, person_trigger, animals_trigger):
            continue
        now = time.monotonic()
        name = _parse_sub_label(after.get('sub_label'))

        # ---- Fast path: personalized greeting OFF -> greet immediately on detection ----
        if not personalized_greeting:
            if etype in ('new', 'update') and label in ('person', 'face'):
                if event_id in fast_fired:
                    continue
                if gate_blocked(label, now):
                    continue
                if now - last_trigger_ts < INTERACTION_COOLDOWN_S:
                    log.info("trigger debounced (cooldown)")
                    continue
                last_trigger_ts = now
                fast_fired.add(event_id)
                log.info("TRIGGER (no recognition wait): %s at the door", label)
                trigger_text = doorman_prompt.interaction_trigger_text(
                    recognized_name=None, doorbell_pressed=False, label=label)
                prompt = doorman_prompt.build_doorman_prompt(recognized_name=None)
                await handle_event(prompt, trigger_text, {'label': label, 'name': None})
            elif etype == 'new' and label in ANIMAL_LABELS:
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
                pending[event_id] = {'new_ts': now, 'name': name, 'label': label}
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
        await _decide(event_id, now, etype)

        # expire stale pending entries
        expired = [eid for eid, p in pending.items()
                   if p.get('triggered', False) and (now - p['new_ts']) > 60]
        for eid in expired:
            del pending[eid]


# ---------------------------------------------------------------- doorbell-press trigger (HA WebSocket)
def _make_ws_backoff(base_s=5.0, max_s=60.0):
    """Exponential backoff + jitter for HA WebSocket reconnects.

    HA auto-bans the source IP on rapid connection churn (returns 403 on the WS
    upgrade and on /api/states). A flat "reconnect in 5s" loop hammers the ban
    and keeps it warm, so the feeder never recovers. This returns (delay, reset):
    delay() yields the next sleep (base*2**n capped at max_s, jittered between
    50% and 100% of the cap so we never retry instantly and avoid thundering),
    and reset() clears the counter after a successful connect so a long-lived
    link keeps the next retry short.
    """
    import random as _random
    _state = {'n': 0}

    def delay():
        cap = min(max_s, base_s * (2 ** _state['n']))
        _state['n'] += 1
        return _random.uniform(0.5 * cap, cap)

    def reset():
        _state['n'] = 0

    return delay, reset


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
    # Reconnect backoff + jitter: HA auto-bans this source IP on rapid connection
    # churn (403 on the WS upgrade / /api/states). A flat 5s retry loop hammers the
    # ban and keeps it warm; exponential backoff lets a brief ban decay first.
    _backoff_delay, _backoff_reset = _make_ws_backoff()
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
                _backoff_reset()

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
            _d = _backoff_delay()
            log.warning("HA WS doorbell listener dropped (%s); reconnecting in %.0fs", e, _d)
            await _aio.sleep(_d)
        except Exception as e:
            _d = _backoff_delay()
            log.error("HA WS doorbell listener error (%s); reconnecting in %.0fs", e, _d)
            await _aio.sleep(_d)


# ---------------------------------------------------------------- door-zone occupancy gate feeder (HA WebSocket)
async def doorbell_press_tracker(sensor='binary_sensor.doorbell_pressed'):
    """Always-on listener that records each doorbell press (off->on) AND release
    (on->off) edge into audio_bridge._PRESS_EDGE (single source of truth, so the
    dual-import of doorman.py can't split state). run_interaction's
    ring_settle_wait() opens the backchannel only after the sensor has returned
    to 'off' AND the minimum post-press window has elapsed (verified 2026-09-25:
    opening bc=1 while the AD410 ring light is still blue wedges the camera's
    RTSP server; after the window it is safe).

    Runs in ALL trigger modes (unlike doorbell_event_listener, which only runs in
    doorbell/hybrid modes) because press-rings can trigger a Doorman interaction
    through the person/motion path too. Event-driven over HA WS so it carries no
    IP-ban polling risk. If HASS_TOKEN is missing it exits quietly."""
    import websockets
    import time as _time
    cfg = _dc.load()
    sensor = sensor or (cfg.get('DOORMAN_DOORBELL_SENSOR') or '').strip()
    hass_ws = (cfg.get('HASS_URL') or 'http://<ha-host>:8123').replace('http://', 'ws://').replace('https://', 'wss://') + '/api/websocket'
    token = cfg.get('HASS_TOKEN') or ''
    if not token:
        log.info("press tracker: no HASS_TOKEN; skipping (ring-settle wait disabled)")
        return
    _backoff_delay, _backoff_reset = _make_ws_backoff()
    while True:
        try:
            async with websockets.connect(hass_ws, max_size=10 * 1024 * 1024, open_timeout=30,
                                          ping_interval=30, ping_timeout=60) as ws:
                msg = json.loads(await ws.recv())
                if msg.get('type') != 'auth_required':
                    raise RuntimeError("HA websocket did not ask for auth")
                await ws.send(json.dumps({'type': 'auth', 'access_token': token}))
                auth = json.loads(await ws.recv())
                if auth.get('type') != 'auth_ok':
                    raise RuntimeError("HA websocket auth failed: %s" % auth)
                await ws.send(json.dumps({"id": 1, "type": "subscribe_events",
                                          "event_type": "state_changed"}))
                sub = json.loads(await ws.recv())
                if not sub.get('success'):
                    raise RuntimeError("subscribe failed")
                log.info("press tracker subscribed to %s", sensor)
                _backoff_reset()
                while True:
                    raw = await ws.recv()
                    try:
                        msg = json.loads(raw)
                    except Exception:
                        continue
                    if msg.get('type') == 'event':
                        data = msg.get('event', {}).get('data', {})
                        if data.get('entity_id') != sensor:
                            continue
                        n = data.get('new_state', {}).get('state')
                        o = data.get('old_state', {}).get('state')
                        if n == 'on' and o != 'on':
                            ab.record_doorbell_press()
                            log.info("press tracker: doorbell press edge recorded")
                        elif n == 'off' and o == 'on':
                            ab.record_doorbell_release()
                            log.info("press tracker: doorbell release edge recorded")
                    elif msg.get('type') == 'ping':
                        await ws.send(json.dumps({'type': 'pong', 'id': msg.get('id')}))
        except asyncio.CancelledError:
            raise
        except websockets.ConnectionClosed as e:
            _d = _backoff_delay()
            log.warning("press tracker WS dropped (%s); reconnecting in %.0fs", e, _d)
            await asyncio.sleep(_d)
        except Exception as e:
            _d = _backoff_delay()
            log.warning("press tracker WS error (%s); reconnecting in %.0fs", str(e)[:80], _d)
            await asyncio.sleep(_d)


async def _seed_gate_from_state(hass_http, sensor, token, gate):
    """One-shot GET /api/states/{sensor} to seed the gate hold on (re)connect.

    Closes the gap where the sensor was already 'on' before the WS subscription
    started: no state_changed then fires, so the hold clock (_on_since) would
    otherwise stay None and a visitor standing still at the door would never
    satisfy the gate. Call this right after each successful subscribe."""
    import aiohttp
    import time as _time
    url = hass_http.rstrip('/') + '/api/states/' + sensor
    headers = {'Authorization': 'Bearer ' + token}
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(url, headers=headers,
                             timeout=aiohttp.ClientTimeout(total=10)) as r:
                if r.status != 200:
                    log.warning("door-zone gate seed: HTTP %s for %s", r.status, sensor)
                    return
                data = await r.json()
    except Exception as e:
        log.warning("door-zone gate seed: %s", e)
        return
    state = (data or {}).get('state')
    now = _time.monotonic()
    if state == 'on':
        gate.mark_on(now)
        log.info("door-zone gate seed: %s is on -> hold started now", sensor)
    else:
        gate.mark_off(now)
        log.info("door-zone gate seed: %s is %r -> hold not started", sensor, state)


async def doorzone_gate_listener(gate, sensor=None):
    """Subscribe to HA state_changed for the door-zone occupancy sensor and feed
    DoorZoneGate.mark_on/mark_off. 'off' and 'unavailable' both reset the hold.
    Same connect/auth/pong/reconnect skeleton as doorbell_event_listener.
    Seeds the hold from a one-shot GET /api/states after each (re)connect so a
    sensor that is already 'on' at subscription time still counts down."""
    import asyncio as _aio
    import websockets
    import json as _json
    import time as _time
    cfg = _dc.load()
    sensor = sensor or (cfg.get('DOORMAN_PERSON_GATE') or '').strip()
    if not sensor:
        return
    hass_http = (cfg.get('HASS_URL') or 'http://<ha-host>:8123').rstrip('/')
    hass_ws = hass_http.replace('http://', 'ws://').replace('https://', 'wss://') + '/api/websocket'
    token = cfg.get('HASS_TOKEN') or ''
    if not token:
        log.warning("door-zone gate: no HASS_TOKEN, gate stays unsatisfied")
        return
    # Reconnect backoff + jitter: same HA IP-ban dynamics as the doorbell listener.
    # A flat 5s loop hammers the ban and the gate hold clock never advances.
    _g_delay, _g_reset = _make_ws_backoff()
    while True:
        try:
            async with websockets.connect(hass_ws, max_size=10 * 1024 * 1024, open_timeout=30,
                                          ping_interval=30, ping_timeout=60) as ws:
                msg = _json.loads(await ws.recv())
                if msg.get('type') != 'auth_required':
                    raise RuntimeError("HA websocket did not ask for auth: %s" % msg)
                await ws.send(_json.dumps({'type': 'auth', 'access_token': token}))
                auth = _json.loads(await ws.recv())
                if auth.get('type') != 'auth_ok':
                    raise RuntimeError("HA websocket auth failed: %s" % auth)
                await ws.send(_json.dumps({'id': 1, 'type': 'subscribe_events', 'event_type': 'state_changed'}))
                sub = _json.loads(await ws.recv())
                if not sub.get('success'):
                    raise RuntimeError("subscribe_events failed: %s" % sub)
                log.info("door-zone gate subscribed: %s", sensor)
                # Seed the hold from the CURRENT state so a sensor already 'on'
                # before this (re)connect still counts down (no state change yet).
                await _seed_gate_from_state(hass_http, sensor, token, gate)
                _g_reset()
                while True:
                    raw = await ws.recv()
                    try:
                        m = _json.loads(raw)
                    except Exception:
                        continue
                    if m.get('type') != 'event':
                        if m.get('type') == 'ping':
                            await ws.send(_json.dumps({'type': 'pong', 'id': m.get('id')}))
                        continue
                    data = m.get('event', {}).get('data', {})
                    if data.get('entity_id') != sensor:
                        continue
                    ns = (data.get('new_state') or {}).get('state')
                    now = _time.monotonic()
                    if ns == 'on':
                        gate.mark_on(now)
                    else:  # off / unavailable / anything else resets the hold
                        gate.mark_off(now)
        except _aio.CancelledError:
            raise
        except websockets.ConnectionClosed as e:
            _d = _g_delay()
            log.warning("door-zone gate WS dropped (%s); reconnecting in %.0fs", e, _d)
            await _aio.sleep(_d)
        except Exception as e:
            _d = _g_delay()
            log.error("door-zone gate WS error (%s); reconnecting in %.0fs", e, _d)
            await _aio.sleep(_d)


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
    global IGNORED_FACES
    IGNORED_FACES = set(cfg.get('DOORMAN_IGNORED_FACES') or set())
    trigger_mode = str(cfg.get('DOORMAN_TRIGGER_MODE', 'person')).strip().lower()
    doorbell_sensor = cfg.get('DOORMAN_DOORBELL_SENSOR') or 'binary_sensor.doorbell_pressed'
    gate_entity = (cfg.get('DOORMAN_PERSON_GATE') or '').strip()
    hold_s = float(cfg.get('DOORMAN_PERSON_HOLD_S', 5.0))
    gate = None
    if gate_entity and trigger_mode in ('person', 'hybrid'):
        gate = DoorZoneGate(gate_entity, hold_s)
        log.info("door-zone gate active: %s held %.0fs", gate_entity, hold_s)
    log.info("personalized greeting enabled: %s", personalized)
    log.info("trigger mode: %s", trigger_mode)
    log.info("animal behavior: %s", cfg.get('DOORMAN_ANIMAL_BEHAVIOR', 'voice'))
    log.info("animal reactions: %s (independent of trigger mode)", ANIMAL_REACTIONS)
    log.info("ignored faces: %s", sorted(IGNORED_FACES) or '(none)')
    busy = asyncio.Event()  # not used to block, but to note a running interaction
    async def handle_event(prompt, trigger_text, meta):
        log.info("TRIGGER: %s", trigger_text)
        label = meta.get('label', 'person')
        behavior = str(cfg.get('DOORMAN_ANIMAL_BEHAVIOR', 'voice') or 'voice').lower()
        action = _decide_trigger_action(label, behavior)
        if action == 'animal-off':
            log.info("animal %s detected; DOORMAN_ANIMAL_BEHAVIOR=off, ignoring", label)
            return
        # Camera health gate. The AD410 wedges its RTSP server around interactions
        # (self-recovers in ~1-5 min) and both cheap proxies lie about it: HA's
        # camera_status sensor polls HTTP :80 and reported `up` through 40s of total
        # RTSP failure, and go2rtc keeps a stale producer record with a populated
        # remote_addr after the camera stops answering. The gate therefore keys off
        # Frigate's actual frame flow (camera_fps/process_fps), the only signal
        # observed to track an outage. Bounded so a ring is never dropped forever.
        try:
            ok = await ab.wait_camera_healthy(max_wait_s=90, poll_s=2, consecutive=1)
            if ok:
                log.info("camera health gate: camera ready")
            else:
                log.warning("camera health gate: timed out; starting anyway")
        except Exception as e:
            log.warning("camera health gate error: %s; proceeding", e)
        if action in ('animal-voice', 'animal-notify'):
            await animal_reaction(label, cfg, behavior)
            log.info("animal reaction done (%s, %s)", label, behavior)
            return
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
    # Always-on: record doorbell press edges so run_interaction can wait out the
    # AD410's post-press window before opening the backchannel (all trigger modes).
    tasks.append(doorbell_press_tracker(sensor=doorbell_sensor))
    if trigger_mode in ('doorbell', 'hybrid'):
        tasks.append(doorbell_event_listener(handle_event, sensor=doorbell_sensor))
    # Person-triggering follows the trigger mode; animal-triggering is the
    # independent DOORMAN_ANIMAL_REACTIONS toggle. The Frigate listener runs
    # whenever EITHER is on, so animals work in any mode without changing it.
    person_trigger = trigger_mode in ('person', 'hybrid')
    animals_trigger = ANIMAL_REACTIONS
    if person_trigger or animals_trigger:
        tasks.append(frigate_event_listener(handle_event,
                                            personalized_greeting=personalized,
                                            gate=gate,
                                            person_trigger=person_trigger,
                                            animals_trigger=animals_trigger))
    if gate is not None:
        tasks.append(doorzone_gate_listener(gate, sensor=gate.entity_id))

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
