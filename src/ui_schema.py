#!/usr/bin/env python3
"""Authoritative Doorman config field registry for the UI.

restart_required = the value is frozen into a module global or the amain-time
cfg snapshot at process start, so it needs a process re-exec to take effect.
False = re-read via a fresh doorman_config.load() at the point of use (hot).
Groups render as form sections.
"""

# field tuple: (key, label, group, type, restart_required, secret, options, default, help)
# type in: text | secret | number | boolean | select
FIELDS = [
    # --- Home Assistant ---
    ('HASS_URL', 'Home Assistant URL', 'Home Assistant', 'text', True, False,
     None, '', 'Base URL, e.g. http://<ha-host>:8123'),
    ('HASS_TOKEN', 'HA long-lived token', 'Home Assistant', 'secret', True, True,
     None, '', 'Bearer token for notify + camera.snapshot'),
    # --- MQTT ---
    ('MQTT_HOST', 'MQTT host', 'MQTT', 'text', True, False, None, '', ''),
    ('MQTT_PORT', 'MQTT port', 'MQTT', 'number', True, False, None, 1883, ''),
    ('MQTT_USER', 'MQTT user', 'MQTT', 'text', True, False, None, '', ''),
    ('MQTT_PASSWORD', 'MQTT password', 'MQTT', 'secret', True, True, None, '', ''),
    # --- Frigate / go2rtc ---
    ('FRIGATE_URL', 'Frigate URL', 'Frigate / go2rtc', 'text', True, False,
     None, '', ''),
    ('GO2RTC_URL', 'go2rtc URL', 'Frigate / go2rtc', 'text', True, False,
     None, '', ''),
    ('FRIGATE_TOPIC', 'Frigate events topic', 'Frigate / go2rtc', 'text', True, False,
     None, '', ''),
    ('FRONT_CAMERA', 'Front camera key', 'Frigate / go2rtc', 'text', True, False,
     None, '', ''),
    # --- Camera & mic ---
    ('DOORMAN_MIC_SOURCE', 'Visitor mic source', 'Camera & mic', 'select', False, False,
     ['relay', 'http', 'webtrack'], 'relay',
     'relay=go2rtc RTSP relay; http=AD410 getAudio (zero-RTSP); webtrack=WebRTC track'),
    ('DOORMAN_MIC_RTSP', 'Mic RTSP relay', 'Camera & mic', 'text', False, False,
     None, '', 'go2rtc relay URL for the visitor mic'),
    ('DOORMAN_DOORBELL_HOST', 'Doorbell host', 'Camera & mic', 'text', False, False,
     None, '', 'AD410 HTTP host (MIC_SOURCE=http)'),
    ('DOORMAN_DOORBELL_USER', 'Doorbell user', 'Camera & mic', 'text', False, False,
     None, '', ''),
    ('DOORMAN_DOORBELL_PASSWORD', 'Doorbell password', 'Camera & mic', 'secret', False, True,
     None, '', ''),
    # --- Gemini ---
    ('GEMINI_API_KEY', 'Gemini API key', 'Gemini Live', 'secret', False, True,
     None, '', ''),
    ('DOORMAN_VOICE', 'Gemini voice', 'Gemini Live', 'text', False, False,
     None, '', 'Optional prebuilt voice name (empty=default)'),
    # --- Voice engine ---
    ('DOORMAN_VOICE_ENGINE', 'Voice engine', 'Voice engine', 'select', True, False,
     ['gemini', 'local'], 'gemini', 'gemini=cloud Live; local=on-LAN STT/LLM/TTS'),
    # --- Local engine: LLM ---
    ('DOORMAN_LLM_BASE_URL', 'LLM base URL', 'Local engine - LLM', 'text', False, False,
     None, '', 'OpenAI-compatible /chat/completions base URL'),
    ('DOORMAN_LLM_MODEL', 'LLM model', 'Local engine - LLM', 'text', False, False,
     None, '', ''),
    ('DOORMAN_LLM_API_KEY', 'LLM API key', 'Local engine - LLM', 'secret', False, True,
     None, '', "'ollama' for Ollama"),
    ('DOORMAN_LLM_KEEP_ALIVE', 'LLM keep-alive', 'Local engine - LLM', 'text', False, False,
     None, '', "Ollama only, e.g. '30m'"),
    ('DOORMAN_LLM_TEMPERATURE', 'LLM temperature', 'Local engine - LLM', 'number', False, False,
     None, 0.8, ''),
    ('DOORMAN_LLM_MAX_TOKENS', 'LLM max tokens', 'Local engine - LLM', 'number', False, False,
     None, 400, ''),
    ('DOORMAN_LLM_THINK', 'LLM thinking chain', 'Local engine - LLM', 'boolean', False, False,
     None, False, 'qwen3 thinking; off=snappy door replies'),
    # --- Local engine: TTS ---
    ('DOORMAN_TTS_BASE_URL', 'TTS base URL', 'Local engine - TTS', 'text', False, False,
     None, '', ''),
    ('DOORMAN_TTS_PROFILE_EN', 'TTS EN profile', 'Local engine - TTS', 'text', False, False,
     None, '', ''),
    ('DOORMAN_TTS_PROFILE_ES', 'TTS ES profile', 'Local engine - TTS', 'text', False, False,
     None, '', ''),
    ('DOORMAN_TTS_ENGINE_EN', 'TTS EN engine', 'Local engine - TTS', 'text', False, False,
     None, '', ''),
    ('DOORMAN_TTS_ENGINE_ES', 'TTS ES engine', 'Local engine - TTS', 'text', False, False,
     None, '', ''),
    # --- Local engine: STT ---
    ('DOORMAN_STT_BASE_URL', 'STT base URL', 'Local engine - STT', 'text', False, False,
     None, '', 'Parakeet :10301 or faster-whisper :10302'),
    ('DOORMAN_LOCAL_STT_MAX_NSP', 'STT no-speech prob cap', 'Local engine - STT', 'number', False, False,
     None, 0.25, 'Whisper anti-hallucination gate'),
    # --- Local engine: mic/audio ---
    ('DOORMAN_LOCAL_MIC_GAIN', 'Mic gain (x)', 'Local engine - audio', 'number', False, False,
     None, 40.0, ''),
    ('DOORMAN_LOCAL_MIC_LIMITER', 'Mic limiter', 'Local engine - audio', 'boolean', False, False,
     None, True, 'alimiter after gain; caps close-voice peaks'),
    ('DOORMAN_LOCAL_MIC_STALL_LIMIT_S', 'Mic stall limit s', 'Local engine - audio', 'number', False, False,
     None, 5.0, 'RTSP gap before the ffmpeg pull reopens'),
    ('DOORMAN_LOCAL_MIC_MAX_REOPENS', 'Mic max reopens', 'Local engine - audio', 'number', False, False,
     None, 8, 'ffmpeg reopens per interaction before giving up'),
    ('DOORMAN_LOCAL_VAD_AGGRESSIVENESS', 'VAD aggressiveness', 'Local engine - audio', 'number', False, False,
     None, 2, '0-3, webrtcvad'),
    ('DOORMAN_LOCAL_MIN_RMS', 'Min RMS (voice)', 'Local engine - audio', 'number', False, False,
     None, 500.0, ''),
    ('DOORMAN_LOCAL_SILENCE_MS', 'Endpoint silence ms', 'Local engine - audio', 'number', False, False,
     None, 700, ''),
    ('DOORMAN_LOCAL_MIN_SPEECH_MS', 'Min speech ms', 'Local engine - audio', 'number', False, False,
     None, 300, ''),
    ('DOORMAN_LOCAL_MAX_RING_MS', 'Endpoint ring ms', 'Local engine - audio', 'number', False, False,
     None, 10000, ''),
    ('DOORMAN_LOCAL_DEBUG_CAPTURE', 'Mic debug capture path', 'Local engine - audio', 'text', False, False,
     None, '', 'Empty=no capture; else WAV per interaction'),
    ('DOORMAN_LOCAL_LANG_FALLBACK', 'Language fallback', 'Local engine - audio', 'select', False, False,
     ['auto', 'es'], 'auto', 'auto=EN default; es=all-Spanish'),
    # --- Keep warm ---
    ('DOORMAN_KEEP_WARM_SETTLE_S', 'Keep-warm settle s', 'Local engine - keep-warm', 'number', False, False,
     None, 30, 'Delay before the first warm-up ping'),
    ('DOORMAN_KEEP_WARM_INTERVAL_S', 'Keep-warm interval s', 'Local engine - keep-warm', 'number', False, False,
     None, 1500, 'Repeat warm-up every N seconds'),
    # --- Behaviour / timing ---
    ('DOORMAN_PERSONALIZED_GREETING', 'Personalized greeting', 'Behaviour', 'boolean', True, False,
     None, True, 'Wait for face recognition, greet by name'),
    ('DOORMAN_TRIGGER_MODE', 'Trigger mode', 'Behaviour', 'select', True, False,
     ['person', 'doorbell', 'hybrid'], 'person', ''),
    ('DOORMAN_DOORBELL_SENSOR', 'Doorbell sensor', 'Behaviour', 'text', True, False,
     None, '', 'HA binary_sensor for doorbell mode'),
    ('IDLE_TIMEOUT_S', 'Idle timeout s', 'Behaviour', 'number', True, False,
     None, 25, 'End interaction after this idle'),
    ('INTERACTION_MAX_S', 'Max interaction s', 'Behaviour', 'number', True, False,
     None, 120, ''),
    ('INTERACTION_COOLDOWN_S', 'Cooldown s', 'Behaviour', 'number', True, False,
     None, 20, 'Min gap between interactions'),
    ('DOORMAN_PERSON_GATE', 'Door-zone gate', 'Behaviour', 'text', True, False,
     None, '', 'HA occupancy sensor; empty=off'),
    ('DOORMAN_PERSON_HOLD_S', 'Gate hold s', 'Behaviour', 'number', True, False,
     None, 5, 'Seconds the gate must be held on'),
    ('DOORMAN_RECOGNIZE_GRACE_S', 'Recognize grace s', 'Behaviour', 'number', True, False,
     None, 12, 'Wait for face recognition before greeting as unknown'),
    ('DOORMAN_IGNORED_FACES', 'Ignored faces', 'Behaviour', 'text', True, False,
     None, '', 'Comma-separated Frigate face names to skip'),
    ('DOORMAN_RING_SETTLE_S', 'Ring settle s', 'Behaviour', 'number', True, False,
     None, 12, 'Post-release wait before opening backchannel'),
    ('DOORMAN_RING_SETTLE_MAX_S', 'Ring settle max s', 'Behaviour', 'number', True, False,
     None, 45, 'Cap on the post-press upstream poll'),
    ('DOORMAN_RING_SETTLE_PRESS_GRACE_S', 'Press grace s', 'Behaviour', 'number', True, False,
     None, 5, 'Wait for the HA WS press edge of the same ring'),
    ('DOORMAN_UPSTREAM_CHECK_WINDOW_S', 'Upstream check window s', 'Behaviour', 'number', True, False,
     None, 3, 'go2rtc bytes_recv sample window'),
    ('DOORMAN_UPSTREAM_CHECK_MIN_BYTES', 'Upstream min bytes', 'Behaviour', 'number', True, False,
     None, 50000, 'bytes_recv threshold for a live upstream'),
    # --- Animal ---
    ('DOORMAN_ANIMAL_REACTIONS', 'Animal reactions', 'Animal', 'boolean', True, False,
     None, True, 'React to cat/dog (any trigger mode)'),
    ('DOORMAN_ANIMAL_BEHAVIOR', 'Animal behavior', 'Animal', 'select', True, False,
     ['voice', 'notify', 'off'], 'voice', ''),
    ('DOORMAN_ANIMAL_MAX_S', 'Animal session cap s', 'Animal', 'number', True, False,
     None, 45, ''),
    # --- Snapshots ---
    ('DOORMAN_SNAPSHOT_DIR', 'Snapshot dir', 'Snapshots', 'text', False, False,
     None, '', ''),
    ('DOORMAN_SNAPSHOT_RETENTION', 'Snapshot retention', 'Snapshots', 'number', False, False,
     None, 25, '0=keep all'),
    # --- Legacy mic (fallback) ---
    ('CAM_MIC_RTSP', 'Camera mic RTSP (legacy)', 'Camera & mic', 'text', False, False,
     None, '', 'Direct-camera RTSP; fallback when DOORMAN_MIC_RTSP is unset'),
    # --- Web UI ---
    ('DOORMAN_UI_PORT', 'UI port', 'Web UI', 'number', True, False,
     None, 8090, 'LAN port the in-container UI listens on'),
    ('DOORMAN_UI_ENABLED', 'UI enabled', 'Web UI', 'boolean', True, False,
     None, True, 'false = run the voice service without the UI'),
    ('DOORMAN_DATA_DIR', 'Data dir', 'Web UI', 'text', True, False,
     None, '/data', 'Where transcripts + session snapshots live'),
]


def field_map():
    """{key: spec-dict} for programmatic access."""
    return {k: {'key': k, 'label': l, 'group': g, 'type': t,
                'restart_required': rr, 'secret': s, 'options': o,
                'default': d, 'help': h}
            for (k, l, g, t, rr, s, o, d, h) in FIELDS}


def groups_in_order():
    """Group names in first-appearance order (drives form section order)."""
    seen = []
    for f in FIELDS:
        if f[2] not in seen:
            seen.append(f[2])
    return seen
