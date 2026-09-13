# Doorman Deluxe

> **Public version:** see `ryang3d/doorman-deluxe` (public). This private repo is the
> development source of truth; the public repo is a clean, scrubbed export of `main`
> (no camera password, no test snapshots).

AI-speaking agentic doorbell for a privacy-focused homelab. When someone approaches or rings, Doorman Deluxe talks through the doorbell camera speaker using a Gemini Live two-way voice session, greets them per household policy, can snapshot the visitor, and notifies the homeowner on a phone.

Everything runs locally except the voice engine: Frigate (person/face/cat/dog detection + snapshots), go2rtc WebRTC talkback to the doorbell, Home Assistant (doorbell-press trigger, notifications, camera snapshots), and a Gemini Live cloud voice session.

## Architecture

- Trigger (configurable): Frigate person/face/cat/dog detection via MQTT, or Home Assistant `binary_sensor.doorbell_pressed` via WebSocket. Cat/dog detections get a configurable animal reaction (playful spoken greeting + notification) instead of a full visitor conversation; see `DOORMAN_ANIMAL_BEHAVIOR`.
- Voice: Gemini Live two-way audio (cloud). Mic comes off the doorbell camera RTSP; AI speech is pushed back through go2rtc consumer-mode WebRTC to the doorbell speaker.
- Tools the model can call: snapshot the front door, notify the homeowner.
- Snapshots in notifications are captured by HA's `camera.snapshot` and served at HA `/local` (no SSH, no extra port).

## Requirements

- Python 3.13 + ffmpeg (bare metal) or Docker + docker compose (recommended).
- A doorbell camera with a speaker + mic reachable over RTSP (verified on Amcrest AD410).
- Frigate with go2rtc (bundled) exposing a two-way stream for the doorbell.
- Home Assistant (for notify, camera.snapshot, and optional doorbell-press trigger).
- Google Gemini API key.

## Deploy (docker compose, recommended)

1. Install docker + docker compose plugin.
2. Clone the repo.
3. Copy the env template and set values:

   `cp .env.example .env`

   Required: `DOORMAN_HASS_TOKEN` (HA long-lived token), `DOORMAN_MQTT_USER`/`DOORMAN_MQTT_PASSWORD`, `DOORMAN_GEMINI_API_KEY`, `DOORMAN_CAM_MIC_RTSP` (doorbell mic). Edit the IPs to match your network.

4. Build and start:

   `docker compose up -d --build`

5. Verify:

   `docker compose logs -f doorman`

   Healthy startup logs `personalized greeting enabled: <bool>`, `trigger mode: <mode>`, `animal behavior: <voice|notify|off>`, and `subscribed to frigate/events`.

The compose file uses `network_mode: host` (Linux only). This is required because WebRTC/RTSP to the camera and Frigate must behave like bare metal; bridge networking breaks media.

## Configuration (`.env`)

Key options (see `.env.example` for the full list):

- `DOORMAN_PERSONALIZED_GREETING` - `true` waits for face recognition and greets by name (adds ~10-20s before first speech); `false` greets immediately on detection.
- `DOORMAN_TRIGGER_MODE` - `person` (Frigate detection, default), `doorbell` (HA `binary_sensor.doorbell_pressed`), or `hybrid` (either).
- `DOORMAN_DOORBELL_SENSOR` - HA entity watched in `doorbell` trigger mode.
- `DOORMAN_ANIMAL_BEHAVIOR` - what to do on a Frigate cat/dog detection: `voice` (default, playful spoken greeting + notification), `notify` (notification only), or `off` (ignore animals).
- `DOORMAN_ANIMAL_MAX_S` - hard cap (seconds) on an animal voice session.
- `DOORMAN_PERSON_GATE` - HA occupancy sensor that must be held `on` for `DOORMAN_PERSON_HOLD_S` seconds before a Frigate person/face trigger fires (default: the front-patio person-occupancy zone on the doorbell cam). Set empty to disable the gate (trigger on first detection).
- `DOORMAN_PERSON_HOLD_S` - seconds the door-zone gate sensor must stay `on` before a person/face trigger fires (default `5`). Animals and doorbell presses are unaffected.
- `DOORMAN_IGNORED_FACES` - comma-separated face names to fully ignore (no greeting, no session). Case-insensitive; only applies to the personalized path (`DOORMAN_PERSONALIZED_GREETING=true`) where the recognized name is known. Empty = ignore nobody.
- `DOORMAN_HASS_TOKEN` - HA long-lived access token (notify, camera.snapshot, doorbell trigger, door-zone gate).
- `DOORMAN_VOICE` - optional Gemini prebuilt voice; empty uses the default.
- `DOORMAN_SNAPSHOT_RETENTION` - keep at most this many recent local snapshots (0 = keep all / no pruning).

## Bare-metal deploy (alternative)

Uses the same source without docker:

```
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python src/doorman.py
```

ffmpeg must be installed. Credentials come from environment variables (same `DOORMAN_*` names as `.env`).

## Project layout

- `src/doorman.py` - orchestrator: trigger listeners + voice session + tool dispatch.
- `src/audio_bridge.py` - Gemini Live session, mic capture, talkback, echo gate.
- `src/doorman_tools.py` - snapshot + notify tools, HA-native snapshot.
- `src/doorman_config.py` - config loading (env > profile files > defaults).
- `src/doorman_prompt.py` - household policy persona.
- `docker-compose.yml`, `Dockerfile` - container deployment.
- `tests/` - unit tests (run with `.venv/bin/python tests/<name>.py`).

## Notifications

The model can notify the homeowner via HA `notify.all_devices`. If a doorbell frame is attached, Doorman captures it with HA `camera.snapshot` and serves it at `http://<ha>/local/doorbell/<file>`.

## Testing / troubleshooting

- Door test: `docker compose logs -f doorman` then walk up or ring the bell; watch for `TRIGGER` and `[gemini said]`.
- Unit tests: run from the repo root, one at a time: `.venv/bin/python tests/test_animal_trigger.py` (all config, decision, prompt, and reaction unit tests). Same pattern for `tests/test_config.py`, `tests/test_snapshot.py`, etc.
- Animal test: publish a synthetic Frigate cat event to the MQTT broker and watch for `TRIGGER`, `animal notify`, and `[gemini said]` lines:
  `mosquitto_pub -h <mqtt> -p 1883 -u <user> -P <pass> -t frigate/events -m '{"type":"new","after":{"id":"cat-test-1","camera":"front_doorbell","label":"cat"}}'`
  (Swap `label` to `dog` for a dog. Send a few times to confirm a different greeting line comes out each time.)
- go2rtc talkback notes and verified transport facts: `TRANSPORT.md`.

## License

MIT, see `LICENSE`.
