#!/usr/bin/env python3
"""Doorman central configuration (dockerization task 1).

Reads config from environment variables (DOORMAN_*) with the CURRENT values as
defaults, so bare-metal/venv runs and tests stay green without setting anything.
Precedence (highest first):
  1. Process environment (DOORMAN_* vars, e.g. set by docker-compose env_file)
  2. The legacy profile config files (.env and frigate.env) when present
  3. Hardcoded defaults matching today's working values

Env var naming: DOORMAN_<UPPER_KEY>. A var that is present (even empty) overrides
file/default. Callers use load() once and pass the dict around.
"""
import os

# Legacy config files this profile used (fallback so bare-metal needs no env).
# Adjust these if your profile / config layout differs; when the files don't
# exist they are simply skipped (env vars and the defaults below win).
PROFILE_ENV = os.path.expanduser('~/.hermes/profiles/home-admin/.env')
FRIGATE_ENV = os.path.expanduser('~/.hermes/profiles/home-admin/frigate.env')

# defaults matching the working deployment (2026-09-08)
DEFAULTS = {
    # HA
    'HASS_URL': 'http://<ha-host>:8123',
    'HASS_TOKEN': '',
    # MQTT broker carrying frigate/events
    'MQTT_HOST': '<ha-host>',
    'MQTT_PORT': 1883,
    'MQTT_USER': '',
    'MQTT_PASSWORD': '',
    # Frigate / go2rtc
    'FRIGATE_URL': 'http://<frigate-host>:5001',
    'GO2RTC_URL': 'http://<frigate-host>:1984',
    'FRIGATE_TOPIC': 'frigate/events',
    'FRONT_CAMERA': 'front_doorbell',
    # doorbell camera mic RTSP (visitor audio in)
    'CAM_MIC_RTSP': ('rtsp://admin:<doorbell-pass>@<camera-ip>:554/'
                     'cam/realmonitor?channel=1&subtype=1'),
    # AD410 native HTTP audio (amcrest-intercom HEAR path). Reading the visitor mic
    # over HTTP getAudio opens NO RTSP session, so the doorbell's backchannel/button
    # stay stable even while we talk (the AD410 crashes under concurrent RTSP pulls).
    'DOORMAN_DOORBELL_HOST': '<camera-ip>',
    'DOORMAN_DOORBELL_USER': 'admin',
    'DOORMAN_DOORBELL_PASSWORD': '',
    # Visitor mic source: go2rtc RTSP RELAY, deliberately the MAIN restream
    # (front_doorbell), NOT the sub (front_doorbell_sub).
    #  - Measured 2026-09-10: the main relay carries live camera-mic audio
    #    (pcm_alaw 8000 Hz, mean -70.5 dB, max -53.2 dB at rest).
    #  - Frigate already holds the main camera session open for record+detect, so
    #    go2rtc multiplexes this consumer with NO extra AD410 RTSP session. The sub
    #    relay is unconsumed after the 2026-09-10 detect-input change, so reading it
    #    would open a 3rd camera session (which wedges the AD410).
    # host:port is the go2rtc RTSP relay (Frigate's bundled go2rtc), not the HTTP API.
    'DOORMAN_MIC_RTSP': 'rtsp://<frigate-host>:8554/front_doorbell',
    # Which visitor-mic implementation to use:
    #   relay    (default) read DOORMAN_MIC_RTSP via ffmpeg. Proven to deliver audio.
    #   webtrack consume the twoway WebRTC connection's received-audio track. go2rtc
    #            always offers this track (sendonly PCMA), but it was NEVER observed
    #            to carry the visitor's voice - and its mere presence silently
    #            shadowed the relay path, so Gemini received nothing at all
    #            (2026-09-10: three rings, greeting only, no visitor audio).
    'DOORMAN_MIC_SOURCE': 'relay',
    # Gemini Live
    'GEMINI_API_KEY': '',
    'DOORMAN_VOICE': '',          # optional prebuilt voice name
    # Voice engine selection: 'gemini' (current cloud Live pipeline, DEFAULT,
    # untouched) or 'local' (Parakeet v3 STT + LLM brain + Chatterbox TTS, all on-LAN).
    'DOORMAN_VOICE_ENGINE': 'gemini',
    # Local engine endpoints/params (only used when DOORMAN_VOICE_ENGINE=local).
    # Brain default: SGLang qwen3.8-27b on the LLM host. Fallback: Ollama on
    # the doorman host -> set DOORMAN_LLM_BASE_URL=http://127.0.0.1:11434/v1,
    # DOORMAN_LLM_MODEL=qwen3.5:9b, DOORMAN_LLM_API_KEY='ollama'.
    'DOORMAN_LLM_BASE_URL': 'http://<llm-host>:30000/v1',  # SGLang (the LLM host)
    'DOORMAN_LLM_MODEL': 'qwen3.8-27b',
    'DOORMAN_LLM_API_KEY': '',        # SGLang Bearer key; 'ollama' value for Ollama
    'DOORMAN_LLM_KEEP_ALIVE': '30m',  # Ollama only (ignored by SGLang, harmless)
    # The Spark brain is a qwen3 thinking model. Its reasoning chain consumes the
    # token budget BEFORE the spoken reply: at max_tokens=300 the production
    # config hit finish_reason=length with EMPTY content (13s, no words). With
    # thinking off the same model answers in 1-5s. 2026-09-17: 'liked the
    # responses from Gemini a lot better' = this, plus sampling at temp 0 (the
    # model's formulaic default) -> set temp 0.8 for naturalness.
    'DOORMAN_LLM_TEMPERATURE': 0.8,   # sampling temperature for the spoken reply
    'DOORMAN_LLM_MAX_TOKENS': 400,    # reply budget (thinking off => ample)
    'DOORMAN_LLM_THINK': False,       # qwen3 think chain; off for snappy door replies
    # TTS = Voicebox (Chatterbox, Ryan's clone). POST {url}/generate, request/response.
    'DOORMAN_TTS_BASE_URL': 'http://127.0.0.1:17600',        # voicebox host port
    'DOORMAN_TTS_PROFILE_EN': 'ffadb2a2-cacc-4c7f-8d26-69f7c4c22246',  # 'Ryan G Cloned'
    'DOORMAN_TTS_PROFILE_ES': 'ffadb2a2-cacc-4c7f-8d26-69f7c4c22246',  # same clone; engine differs
    'DOORMAN_TTS_ENGINE_EN': 'chatterbox_turbo',              # 2.0s warm, EN
    'DOORMAN_TTS_ENGINE_ES': 'chatterbox',                    # 4.0s warm, 23 langs incl. es
    # TTS transport mode: 'voicebox' (default, POST /generate + poll) or 'bare'
    # (a thin Chatterbox-Turbo service that returns the WAV synchronously, see
    # Task 3e). 'bare' is the tier-2 swap-in; it skips the poll loop in synthesize().
    # LOCAL-ENGINE ONLY: DOORMAN_VOICE_ENGINE=gemini ignores this key entirely —
    # the cloud Live path is byte-for-byte unchanged regardless of this value.
    # STT = standalone Parakeet v3 service we deploy (Task 2).
    'DOORMAN_STT_BASE_URL': 'http://127.0.0.1:10301',
    # Keep-warm cadence (seconds). Pings TTS + STT so cold starts (~22s TTS /
    # ~9s STT) never land on a visitor. 0 on the interval disables the loop.
    'DOORMAN_KEEP_WARM_SETTLE_S': 30,        # delay before the first warm-up ping
    'DOORMAN_KEEP_WARM_INTERVAL_S': 1500,    # repeat every 25m while warm
    'DOORMAN_LOCAL_SILENCE_MS': 700,    # endpoint: silence after speech to finalize
    'DOORMAN_LOCAL_MIN_SPEECH_MS': 300, # ignore blips shorter than this
    # VAD aggressiveness (webrtcvad, 0-3; 3 = most aggressive, drops non-speech hard).
    # The doorbell mic is quiet; 3 was too strict in the 2026-09-17 live test (30 s of
    # talk flagged only 7 frames). 2 is the typical default and catches quiet speech.
    'DOORMAN_LOCAL_VAD_AGGRESSIVENESS': 2,
    # Mic gain (linear multiplier, applied in ffmpeg -af volume=). The door mic
    # ambient level is ~RMS 8 of 32768, so 40 (~32 dB) brings quiet speech into
    # the VAD/STT sweet spot. 1.0 = no gain. LOCAL ENGINE ONLY.
    'DOORMAN_LOCAL_MIC_GAIN': 40.0,
    # Fast alimiter after the gain filter so a visitor talking close to the
    # doorbell is capped instead of hard-clipping at the int16 ceiling. A close
    # voice drives the substream to 32767 at 10x gain, which parakeet mangles
    # ('I have a delivery' -> 'I haven't delivered'). limit=0.95, 5ms attack.
    # Only applies to the LOCAL engine mic (gain>0). 'false' to disable.
    'DOORMAN_LOCAL_MIC_LIMITER': True,
    # Debug: when set to a path, the raw (gained) mic stream is also written to
    # that WAV file for each local interaction, so mic tuning can be done
    # offline against real door audio. Empty = no capture. LOCAL ENGINE ONLY.
    'DOORMAN_LOCAL_DEBUG_CAPTURE': '',
    # Endpoint ring size (ms). Must hold a FULL utterance (~10 s) so it is not
    # truncated to its last ~390 ms (the old ~1.1 s ring chopped every utterance
    # to its tail word -> STT only heard "Yeah."/"Oh."). LOCAL ENGINE ONLY.
    'DOORMAN_LOCAL_MAX_RING_MS': 10000,
    # Minimum mean RMS for a VAD-flagged segment to count as voice. The door
    # mic's ambient false-positives are RMS ~300-400; real voice is RMS 9000+.
    # 500 sits between, so noise blips don't fire the endpointer. LOCAL ONLY.
    'DOORMAN_LOCAL_MIN_RMS': 500.0,
    # Parakeet v3 transcribes multilingual audio but does NOT label the language on
    # its Hypothesis result (confirmed 2026-09-16: EncDecRNNTBPEModel has no
    # language field), so the STT 'language' value is always empty. This key sets
    # the language passed to the brain + TTS routing: 'auto' -> EN engine
    # (default, the most common case); 'es' -> ES engine (chatterbox Multilingual)
    # so the brain is told to reply in Spanish. Flip to 'es' for an all-Spanish
    # household; per-utterance switching needs a language detector (out of scope).
    'DOORMAN_LOCAL_LANG_FALLBACK': 'auto',
    # Anti-hallucination gate for the LOCAL STT engine. When STT is Whisper, the
    # service returns a per-utterance no_speech_prob (max over kept segments).
    # Real door speech measures ~0.00-0.05 on this mic; ambient-noise
    # hallucinations ~0.30+. An utterance with no_speech_prob above this value is
    # dropped before it reaches the brain (no fake "visitor said", no idle reset).
    # Parakeet returns no such field, so this is a no-op there. Default 0.25.
    'DOORMAN_LOCAL_STT_MAX_NSP': 0.25,
    # Mic-stall recovery. The doorbell's RTSP audio relay wedges under concurrent
    # stream load and delivers audio in bursts with 4-12 s gaps (verified
    # 2026-09-19). On a gap this long the ffmpeg pull is killed and reopened so
    # the next words aren't swallowed. Lower = more aggressive reopens (risk:
    # chatty reopens on brief drops); higher = fewer reopens (risk: more words
    # lost per stall). Default 5.0 s.
    'DOORMAN_LOCAL_MIC_STALL_LIMIT_S': 5.0,
    # Max ffmpeg reopens per interaction before giving up (dead-source bound).
    # Default 8.
    'DOORMAN_LOCAL_MIC_MAX_REOPENS': 8,
    # behaviour
    'DOORMAN_PERSONALIZED_GREETING': 'true',
    'DOORMAN_ANIMAL_BEHAVIOR': 'voice',
    'DOORMAN_ANIMAL_MAX_S': 45.0,
    # Master on/off for animal reactions (cat/dog Frigate detections). Independent
    # of DOORMAN_TRIGGER_MODE: when True, the Frigate listener runs in ANY mode so
    # animal events are seen, and each triggers the reaction per
    # DOORMAN_ANIMAL_BEHAVIOR. When False, animal events are ignored regardless of
    # mode (and the Frigate listener only runs if person-triggering needs it).
    'DOORMAN_ANIMAL_REACTIONS': True,
    # person = trigger on Frigate person detection (default); doorbell = trigger on
    # the HA doorbell_pressed binary_sensor going on; hybrid = trigger on either.
    'DOORMAN_TRIGGER_MODE': 'person',
    'DOORMAN_DOORBELL_SENSOR': 'binary_sensor.doorbell_pressed',
    'IDLE_TIMEOUT_S': 25.0,
    'INTERACTION_MAX_S': 120.0,
    'INTERACTION_COOLDOWN_S': 20.0,
    # Door-zone gate (supplemental, not a trigger): an HA occupancy sensor that
    # must have been continuously 'on' for DOORMAN_PERSON_HOLD_S seconds before
    # a Frigate person/face detection may trigger. Empty = gate disabled.
    'DOORMAN_PERSON_GATE': 'binary_sensor.front_patio_motion_zone_person_occupancy',
    'DOORMAN_PERSON_HOLD_S': 5.0,
    # Post-press ring-settle gate (audio_bridge.ring_settle_wait): hold this many
    # seconds AFTER the button release edge before opening the two-way
    # backchannel (AD410 blue-window guard). Then poll go2rtc upstream bytes
    # until the main stream resumes, capped at DOORMAN_RING_SETTLE_MAX_S.
    # 0 = no fixed floor; the upstream poll is the only gate.
    'DOORMAN_RING_SETTLE_S': 12.0,
    'DOORMAN_RING_SETTLE_MAX_S': 45.0,
    # Wait this long for the HA WS press edge of the same ring to arrive when
    # no press has been tracked yet (pure-motion rings pay this one small
    # delay before the backchannel may open). 0 = open immediately.
    'DOORMAN_RING_SETTLE_PRESS_GRACE_S': 5.0,
    # main_upstream_receiving window/threshold (the post-press upstream poll).
    # window_s = how long to sample go2rtc bytes_recv; 3s is plenty to catch a
    # wedged camera (flat counter) while staying fast. min_bytes scales with
    # resolution/bitrate (50KB works at 2K and stays safe at 1080p/4K).
    'DOORMAN_UPSTREAM_CHECK_WINDOW_S': 3.0,
    'DOORMAN_UPSTREAM_CHECK_MIN_BYTES': 50_000,
    # How long to wait for Frigate face recognition after a person is detected,
    # before triggering as an unknown visitor. Frigate only lands a sub_label on
    # a minority of front-door events (stationary person -> soft face crop), so a
    # long wait mostly wastes time. 12 s is a good default: if recognition lands
    # it's usually within ~5-10 s; otherwise the doorman greets as an unknown.
    # Re-checks every 5 s (ticker), so effective worst case is ~grace + 5 s.
    'DOORMAN_RECOGNIZE_GRACE_S': 12.0,
    # comma-separated Frigate face names to fully ignore (no greeting). Case-insensitive,
    # matched against the recognized sub_label name. Empty = ignore nobody.
    'DOORMAN_IGNORED_FACES': '',
    # snapshots
    'DOORMAN_SNAPSHOT_DIR': '~/doorman/snapshots',
    # keep at most this many recent snapshots in the dir (0 = keep all / no pruning)
    'DOORMAN_SNAPSHOT_RETENTION': 25,
    # web UI (in-container aiohttp app; network_mode host => LAN reachable)
    'DOORMAN_DATA_DIR': '/data',
    'DOORMAN_UI_PORT': 8090,
    'DOORMAN_UI_ENABLED': True,
    # legacy file paths (only used as fallback sources, not needed in docker)
    'PROFILE_ENV': PROFILE_ENV,
    'FRIGATE_ENV': FRIGATE_ENV,
}


def _read_kv_file(path):
    """Read key=value lines from a file into a dict (skip # and empty)."""
    out = {}
    try:
        for line in open(path):
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            if '=' in line:
                k, v = line.split('=', 1)
                out[k.strip()] = v.strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    return out


def load():
    """Return the merged config dict (env > profile files > defaults)."""
    merged = dict(DEFAULTS)
    # 2. profile files provide the legacy names (HASS_*, FRIGATE_*, GEMINI_*, MQTT_*)
    file_cfg = {}
    file_cfg.update(_read_kv_file(PROFILE_ENV))
    file_cfg.update(_read_kv_file(FRIGATE_ENV))
    # map legacy file keys onto our internal keys where the name matches or maps
    legacy_map = {
        'HASS_URL': 'HASS_URL', 'HASS_TOKEN': 'HASS_TOKEN',
        'MQTT_HOST': 'MQTT_HOST', 'MQTT_PORT': 'MQTT_PORT',
        'MQTT_USER': 'MQTT_USER', 'MQTT_PASSWORD': 'MQTT_PASSWORD',
        'FRIGATE_URL': 'FRIGATE_URL', 'FRIGATE_TOPIC': 'FRIGATE_TOPIC',
        'GO2RTC_URL': 'GO2RTC_URL',
        'FRONT_CAMERA': 'FRONT_CAMERA', 'CAM_MIC_RTSP': 'CAM_MIC_RTSP',
        'GEMINI_API_KEY': 'GEMINI_API_KEY', 'DOORMAN_VOICE': 'DOORMAN_VOICE',
        'DOORMAN_PERSONALIZED_GREETING': 'DOORMAN_PERSONALIZED_GREETING',
        'DOORMAN_ANIMAL_REACTIONS': 'DOORMAN_ANIMAL_REACTIONS',
        'DOORMAN_ANIMAL_BEHAVIOR': 'DOORMAN_ANIMAL_BEHAVIOR',
        'DOORMAN_ANIMAL_MAX_S': 'DOORMAN_ANIMAL_MAX_S',
        'DOORMAN_TRIGGER_MODE': 'DOORMAN_TRIGGER_MODE',
        'DOORMAN_DOORBELL_SENSOR': 'DOORMAN_DOORBELL_SENSOR',
        'DOORMAN_PERSON_GATE': 'DOORMAN_PERSON_GATE',
        'DOORMAN_PERSON_HOLD_S': 'DOORMAN_PERSON_HOLD_S',
        'DOORMAN_RECOGNIZE_GRACE_S': 'DOORMAN_RECOGNIZE_GRACE_S',
        'DOORMAN_DOORBELL_HOST': 'DOORMAN_DOORBELL_HOST',
        'DOORMAN_DOORBELL_USER': 'DOORMAN_DOORBELL_USER',
        'DOORMAN_DOORBELL_PASSWORD': 'DOORMAN_DOORBELL_PASSWORD',
        'DOORMAN_MIC_RTSP': 'DOORMAN_MIC_RTSP',
        'DOORMAN_VOICE_ENGINE': 'DOORMAN_VOICE_ENGINE',
        'DOORMAN_LLM_BASE_URL': 'DOORMAN_LLM_BASE_URL',
        'DOORMAN_LLM_MODEL': 'DOORMAN_LLM_MODEL',
        'DOORMAN_LLM_API_KEY': 'DOORMAN_LLM_API_KEY',
        'DOORMAN_LLM_KEEP_ALIVE': 'DOORMAN_LLM_KEEP_ALIVE',
        'DOORMAN_LLM_TEMPERATURE': 'DOORMAN_LLM_TEMPERATURE',
        'DOORMAN_LLM_MAX_TOKENS': 'DOORMAN_LLM_MAX_TOKENS',
        'DOORMAN_LLM_THINK': 'DOORMAN_LLM_THINK',
        'DOORMAN_TTS_BASE_URL': 'DOORMAN_TTS_BASE_URL',
        'DOORMAN_TTS_PROFILE_EN': 'DOORMAN_TTS_PROFILE_EN',
        'DOORMAN_TTS_PROFILE_ES': 'DOORMAN_TTS_PROFILE_ES',
        'DOORMAN_TTS_ENGINE_EN': 'DOORMAN_TTS_ENGINE_EN',
        'DOORMAN_TTS_ENGINE_ES': 'DOORMAN_TTS_ENGINE_ES',
        'DOORMAN_STT_BASE_URL': 'DOORMAN_STT_BASE_URL',
        'DOORMAN_KEEP_WARM_SETTLE_S': 'DOORMAN_KEEP_WARM_SETTLE_S',
        'DOORMAN_KEEP_WARM_INTERVAL_S': 'DOORMAN_KEEP_WARM_INTERVAL_S',
        'DOORMAN_LOCAL_SILENCE_MS': 'DOORMAN_LOCAL_SILENCE_MS',
        'DOORMAN_LOCAL_MIN_SPEECH_MS': 'DOORMAN_LOCAL_MIN_SPEECH_MS',
        'DOORMAN_LOCAL_VAD_AGGRESSIVENESS': 'DOORMAN_LOCAL_VAD_AGGRESSIVENESS',
        'DOORMAN_LOCAL_MIC_GAIN': 'DOORMAN_LOCAL_MIC_GAIN',
        'DOORMAN_LOCAL_MIC_LIMITER': 'DOORMAN_LOCAL_MIC_LIMITER',
        'DOORMAN_LOCAL_DEBUG_CAPTURE': 'DOORMAN_LOCAL_DEBUG_CAPTURE',
        'DOORMAN_LOCAL_MAX_RING_MS': 'DOORMAN_LOCAL_MAX_RING_MS',
        'DOORMAN_LOCAL_MIN_RMS': 'DOORMAN_LOCAL_MIN_RMS',
        'DOORMAN_LOCAL_LANG_FALLBACK': 'DOORMAN_LOCAL_LANG_FALLBACK',
        'IDLE_TIMEOUT_S': 'IDLE_TIMEOUT_S', 'INTERACTION_MAX_S': 'INTERACTION_MAX_S',
        'INTERACTION_COOLDOWN_S': 'INTERACTION_COOLDOWN_S',
    }
    for src_key, dst_key in legacy_map.items():
        if src_key in file_cfg and file_cfg[src_key] not in (None, ''):
            merged[dst_key] = file_cfg[src_key]

    # 1. env overrides (highest)
    for key in list(DEFAULTS.keys()):
        env_key = key if key.startswith('DOORMAN_') else 'DOORMAN_' + key
        # also accept exact legacy env names for the legacy keys
        candidates = [env_key, key]
        for c in candidates:
            if c in os.environ:
                merged[key] = os.environ[c]
                break

    # normalise numerics / booleans
    for numk in ('MQTT_PORT', 'IDLE_TIMEOUT_S', 'INTERACTION_MAX_S',
                 'INTERACTION_COOLDOWN_S', 'DOORMAN_ANIMAL_MAX_S',
                 'DOORMAN_PERSON_HOLD_S'):
        try:
            merged[numk] = float(merged[numk]) if numk != 'MQTT_PORT' else int(float(merged[numk]))
            if numk == 'MQTT_PORT':
                merged[numk] = int(merged[numk])
        except (TypeError, ValueError):
            pass
    # integer keys (arrive as str from env/files; must be int for sleep()/range())
    for ink in ('DOORMAN_KEEP_WARM_SETTLE_S', 'DOORMAN_KEEP_WARM_INTERVAL_S',
                'DOORMAN_LOCAL_SILENCE_MS', 'DOORMAN_LOCAL_MIN_SPEECH_MS',
                'DOORMAN_LOCAL_MAX_RING_MS'):
        try:
            merged[ink] = int(float(merged[ink]))
        except (TypeError, ValueError):
            pass
    # float keys that arrive as str from env/files
    for fltk in ('DOORMAN_LOCAL_MIC_GAIN', 'DOORMAN_LOCAL_MIN_RMS',
                 'DOORMAN_RECOGNIZE_GRACE_S', 'DOORMAN_LLM_TEMPERATURE'):
        try:
            merged[fltk] = float(merged[fltk])
        except (TypeError, ValueError):
            pass
    # LLM max_tokens: int
    try:
        merged['DOORMAN_LLM_MAX_TOKENS'] = int(merged['DOORMAN_LLM_MAX_TOKENS'])
    except (TypeError, ValueError):
        pass
    # LLM think chain: bool (qwen3 thinking; off = snappy spoken replies)
    merged['DOORMAN_LLM_THINK'] = str(
        merged['DOORMAN_LLM_THINK']).strip().lower() in ('true', '1', 'yes', 'on')
    # Local-mic limiter: bool (caps close-voice peaks instead of hard-clipping)
    merged['DOORMAN_LOCAL_MIC_LIMITER'] = str(
        merged.get('DOORMAN_LOCAL_MIC_LIMITER', 'true')).strip().lower() in ('true', '1', 'yes', 'on')
    # Animal reactions master switch: bool (independent of trigger mode)
    merged['DOORMAN_ANIMAL_REACTIONS'] = str(
        merged.get('DOORMAN_ANIMAL_REACTIONS', 'true')).strip().lower() in ('true', '1', 'yes', 'on')
    # Web UI toggles
    merged['DOORMAN_UI_ENABLED'] = str(
        merged.get('DOORMAN_UI_ENABLED', 'true')).strip().lower() in ('true', '1', 'yes', 'on')
    try:
        merged['DOORMAN_UI_PORT'] = int(float(merged['DOORMAN_UI_PORT']))
    except (TypeError, ValueError):
        merged['DOORMAN_UI_PORT'] = 8090
    merged['DOORMAN_PERSONALIZED_GREETING'] = str(
        merged['DOORMAN_PERSONALIZED_GREETING']).strip().lower() in ('true', '1', 'yes', 'on')
    # DOORMAN_IGNORED_FACES: comma-separated, case-insensitive, de-duplicated name set.
    # Empty/blank -> empty set (feature off).
    merged['DOORMAN_IGNORED_FACES'] = {
        part.strip().lower() for part in
        str(merged.get('DOORMAN_IGNORED_FACES', '')).split(',')
        if part.strip()
    }
    return merged


# Path the UI writes to / reads from (bind-mounted host .env inside the container).
# Overridable for bare-metal/tests via DOORMAN_ENV_FILE.
ENV_FILE_PATH = os.path.expanduser(os.environ.get('DOORMAN_ENV_FILE', '/app/.env'))


def _env_namespace():
    """Keys owned by the .env file (the UI-editable set). These are exactly the
    keys the UI writes, so the file is authoritative for them on a re-exec.
    Keys outside this namespace (set by Dockerfile ENV / compose environment,
    e.g. DOORMAN_SNAPSHOT_DIR when that is the only source) are left alone."""
    try:
        import ui_schema
        return set(ui_schema.field_map().keys())
    except Exception:
        return set()


def load_env_file(path=None):
    """Make the .env file the source of truth for the keys it owns.

    For every key in the .env namespace (the UI-editable set): set it from the
    file if present, otherwise REMOVE it from os.environ so a re-exec doesn't
    linger on a value that was deleted from the file. Keys outside the
    namespace (Dockerfile ENV / compose environment) are untouched. This is what
    makes a UI 'restart' (in-place os.execv) apply every setting without a
    container recreate. Safe to call repeatedly; returns the parsed dict
    (empty if the file is missing).
    """
    path = path or ENV_FILE_PATH
    kv = _read_kv_file(path)   # reuse the #/blank/comment-aware reader
    if not os.path.exists(path):
        return kv              # missing file -> pure no-op (nothing set/dropped)
    namespace = _env_namespace()
    for k, v in kv.items():
        os.environ[k] = v
    # drop namespace keys that were removed from the file (file wins, both ways)
    for k in namespace:
        if k not in kv and k in os.environ:
            del os.environ[k]
    return kv
