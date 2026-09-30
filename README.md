# Doorman Deluxe

> **Public version:** see `ryang3d/doorman-deluxe` (public). This private repo is the
> development source of truth; the public repo is a clean, scrubbed export of `main`
> (no camera password, no test snapshots).

AI-speaking agentic doorbell for a privacy-focused homelab. When someone approaches or rings, Doorman Deluxe talks through the doorbell camera speaker, greets them per household policy, can snapshot the visitor, and notifies the homeowner on a phone.

Everything runs locally, including the voice engine, which has two options:

- **Gemini Live (default)** - two-way cloud voice session: mic off the doorbell camera RTSP, AI speech pushed back through go2rtc consumer-mode WebRTC to the doorbell speaker.
- **Local voice engine** (`DOORMAN_VOICE_ENGINE=local`) - fully on-LAN STT -> LLM -> TTS pipeline (no cloud voice call). See "Local voice engine" below.

The rest of the stack is the same either way: Frigate (person/face/cat/dog detection + snapshots), go2rtc WebRTC talkback to the doorbell, and Home Assistant (doorbell-press trigger, notifications, camera snapshots).

## Origin

Doorman Deluxe implements the privacy-focused, agentic doorbell that Matt Cool described and
proved out in his own environment. See his write-up:
[Doorman: A Privacy-Focused Agentic Doorbell](https://mattcool.tech/posts/doorman-a-privacy-focused-agentic-doorbell).
Thanks to Matt Cool for coming up with the concept, proving it out, and inspiring me to build my
own version.

## Architecture

- Trigger (configurable): Frigate person/face/cat/dog detection via MQTT, or Home Assistant `binary_sensor.doorbell_pressed` via WebSocket. Cat/dog detections get a configurable animal reaction (playful spoken greeting + notification) instead of a full visitor conversation; see `DOORMAN_ANIMAL_BEHAVIOR`.
- Voice: two selectable engines via `DOORMAN_VOICE_ENGINE` -
  - `gemini` (default): Gemini Live two-way audio (cloud). Mic comes off the doorbell camera RTSP; AI speech is pushed back through go2rtc consumer-mode WebRTC to the doorbell speaker.
  - `local`: on-LAN STT -> LLM -> TTS (faster-whisper or Parakeet STT, OpenAI-compatible LLM brain, Chatterbox TTS). Mic capture and go2rtc talkback are the same; no cloud voice call.
- Tools the model can call: snapshot the front door, notify the homeowner.
- Snapshots in notifications are captured by HA's `camera.snapshot` and served at HA `/local` (no SSH, no extra port).

## Requirements

- Python 3.13 + ffmpeg (bare metal) or Docker + docker compose (recommended).
- A doorbell camera with a speaker + mic reachable over RTSP (verified on Amcrest AD410).
- Frigate with go2rtc (bundled) exposing a two-way stream for the doorbell.
- Home Assistant (for notify, camera.snapshot, and optional doorbell-press trigger).
- **Gemini engine:** Google Gemini API key.
- **Local engine** (`DOORMAN_VOICE_ENGINE=local`): an STT service (faster-whisper `:10302` recommended, or Parakeet `:10301` - both bundled in `docker-compose.yml`), an OpenAI-compatible LLM endpoint (`/chat/completions` with tool calling - SGLang/Ollama/vLLM, external), and a TTS endpoint (Voicebox serving Chatterbox, external).

### Frigate version requirement

- **Frigate 0.18.0 or newer** (go2rtc 1.9.14+): the talkback WebRTC socket is
  expected at `ws://<frigate-host>:5001/live/webrtc/api/ws?src=<stream>`.
- **Frigate 0.17.x only** (go2rtc 1.9.10): the socket lives at
  `ws://<frigate-host>:5001/api/go2rtc/api/ws?src=<stream>` instead. 0.18.0
  moved go2rtc behind a separate nginx upstream; the old path now falls
  through to the Frigate app and returns HTTP 403, so Doorman silently skips
  every greeting (log: `talkback connect failed after 3 attempts`). To run on
  0.17.x, change the `ws_url` line in `src/audio_bridge.py` back to the
  `/api/go2rtc/api/ws` path. No authentication (cookie or token) is needed on
  either path.

## Camera prerequisites (Amcrest AD410)

Doorman is verified on the Amcrest AD410. These camera-side settings matter and
are not set by anything in this repo:

- **Disable "Record Audio" (the camera's own local audio recording).** This is
  the most important AD410-specific prerequisite. The AD410 runs a
  single-threaded RTSP server and only ever allows one backchannel (`bc=1`)
  session at a time; when Doorman opens its two-way session, the camera's own
  audio recording competes for the same audio path and is a major contributor
  to the camera wedging (main stream dies, RTSP `i/o timeout` / no-route-to-host)
  under the concurrent load of Frigate's one-way record/detect plus Doorman's
  two-way. Turning off the camera's internal audio recording removes that load.
  Doorman's own mic and talkback are unaffected (they ride the go2rtc RTSP
  sessions, not the camera's internal recorder).

  - Via the **Amcrest Smart Home app**: the camera's recording settings →
    "Record Audio" off.
  - Via the camera's CGI API (the built-in web UI is unusable on the AD410 —
    `merge.js` 404, the known Amcrest bug `rroller/dahua#134` — so use the app
    or this): the endpoint is `configManager.cgi` under HTTP Digest auth, using
    the camera's RTSP credentials:

    ```sh
    # disable the camera's local audio recording
    curl -sg --digest -u "admin:<doorbell-pass>" \
      "http://<camera-ip>/cgi-bin/configManager.cgi?action=setConfig&Record[0].SaveAudio=false"
    # returns: OK
    ```

  - **Resolution:** the AD410's main stream defaults to 2560x1920. Running it
    at 1920x1080 reduces the camera's encode load and makes post-press recovery
    faster. This is optional but recommended; set it in the app or via the
    camera's video settings.

- **Post-press "blue window":** for ~7-8 s after a doorbell press the AD410
  degrades its main stream (the ring light turns green→blue as it enters native
  two-way voice mode). Dialing a second backchannel session into that window
  wedges the camera. Doorman's `ring_settle_wait` gate handles this for you
  (it waits out the window and confirms the main stream is delivering before
  opening the two-way session), so no camera-side change is needed — it's here
  for context when diagnosing timing.

- **One backchannel at a time:** the camera allows exactly one `bc=1` session.
  Doorman is built around this (a single two-way go2rtc stream multiplexing
  mic-in, AI-out, and the camera mic). Don't add a second two-way consumer
  (e.g. a second intercom app or a Frigate stream pointed at
  `#backchannel=1`) while Doorman is live.

- **Firmware:** verified on `1.000.00AC002`. Newer firmwares may change the
  backchannel behavior; if the camera starts wedging after an update, re-check
  the settings above.

## Deploy (docker compose, recommended)

1. Install docker + docker compose plugin.
2. Clone the repo.
3. Copy the env template and set values:

   `cp .env.example .env`

   Required: `DOORMAN_HASS_TOKEN` (HA long-lived token), `DOORMAN_MQTT_USER`/`DOORMAN_MQTT_PASSWORD`, `DOORMAN_GEMINI_API_KEY`, `DOORMAN_CAM_MIC_RTSP` (doorbell mic). Edit the IPs to match your network.

4. Build and start:

   `docker compose up -d --build`

   This starts `doorman` plus the two optional STT services (`whisper` on `:10302`,
   `parakeet` on `:10301`). `doorman` only uses one of them when
   `DOORMAN_VOICE_ENGINE=local`; for the default Gemini engine you can skip them with
   `docker compose up -d --build doorman` to save the GPU VRAM they would otherwise reserve.

5. Verify:

   `docker compose logs -f doorman`

   Healthy startup logs `personalized greeting enabled: <bool>`, `trigger mode: <mode>`, `animal behavior: <voice|notify|off>`, and `subscribed to frigate/events`. With `DOORMAN_VOICE_ENGINE=local` it additionally logs `voice engine: local`.

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
- `DOORMAN_VOICE_ENGINE` - `gemini` (default) or `local`. See the "Local voice engine" section for the local-only keys.

## Local voice engine (`DOORMAN_VOICE_ENGINE=local`)

When the engine is `local`, Doorman runs the whole conversation on the LAN instead of a
Gemini Live session: door audio -> **STT** -> **LLM brain** (household policy, tool
calling) -> **TTS**, with the same go2rtc talkback and the same tools (snapshot + notify).
Both engines share the same household-policy persona; the local engine only gets a short
addendum that keeps replies short enough for TTS.

The STT services in `docker-compose.yml` are independent of each other and of `doorman` -
bring up only what you need:

`docker compose up -d whisper` # faster-whisper small.en on :10302 (recommended STT)

`docker compose up -d parakeet` # Parakeet TDT 0.6B v3 on :10301 (alternative STT)

The LLM brain and TTS are external to this compose file (your SGLang/Ollama instance and a
Voicebox box); Doorman reaches them over the LAN via the `DOORMAN_LLM_*` / `DOORMAN_TTS_*`
keys.

STT: both services expose `POST /transcribe`; point `DOORMAN_STT_BASE_URL` at whichever
is running. faster-whisper is the recommended choice for the noisy far-field door mic -
it returns `no_speech_prob` and `language`, which Doorman uses for an anti-hallucination
gate (`DOORMAN_LOCAL_STT_MAX_NSP`; transcriptions with a higher `no_speech_prob` are
dropped before they reach the brain). Parakeet returns no such field and the gate is a
no-op for it.

LLM brain: any OpenAI-compatible `/chat/completions` endpoint with tool support
(`DOORMAN_LLM_BASE_URL` / `DOORMAN_LLM_MODEL` / `DOORMAN_LLM_API_KEY`). Tunes:
`DOORMAN_LLM_TEMPERATURE`, `DOORMAN_LLM_MAX_TOKENS`, `DOORMAN_LLM_THINK` (qwen3 think
chain; off for snappy door replies).

TTS: Voicebox serving Chatterbox (`DOORMAN_TTS_BASE_URL`, plus per-language
profile/engine keys: `chatterbox_turbo` for EN, `chatterbox` for ES).

Local-engine audio tuning (all in `.env.example` with defaults):
`DOORMAN_LOCAL_STT_MAX_NSP` (anti-hallucination gate, see above),
`DOORMAN_LOCAL_MIC_STALL_LIMIT_S` (the doorbell RTSP relay wedges under concurrent stream
load and delivers audio in 4-12 s bursts; on a gap this long the ffmpeg mic pull is
killed and reopened so the next words aren't swallowed; default 5.0 s) and
`DOORMAN_LOCAL_MIC_MAX_REOPENS` (cap on ffmpeg reopens per interaction before the source
is treated as dead; default 8).

## Web UI

An in-container web UI (aiohttp, no build step, offline) ships with the service: status dashboard, visit history with per-visit snapshot + recognized name, timestamped conversation transcripts, and a settings form covering every `DOORMAN_*` key.

- **Reach it** at `http://<doorman-host>:8090` (the service uses `network_mode: host`, so the bind port is directly reachable). No auth - trusted LAN only. Disable with `DOORMAN_UI_ENABLED=false` (voice service only) or move it with `DOORMAN_UI_PORT`.
- **Tabs:** Dashboard (camera health, engine, trigger, last visit, live "in a conversation" indicator + live transcript), History (visits with snapshot thumbnails, click through to the transcript), Settings (all keys, grouped, with hot/restart badges).
- **Settings model.** Saving writes `.env`. Keys are either *hot* (re-read at the point of use, so the change takes effect on the next interaction without a restart) or *restart-required* (frozen into the process at start). The form flags each, and when a restart is needed it offers **Restart to apply**, which re-execs the process in place (same container - no recreate). `.env` is the source of truth both ways: a key present in the file wins, and a key removed from the file is dropped on the next start.
- **Transcripts** are recorded from both voice engines (the same spots that log `[visitor said]` / `[doorman said]` / `[tool call]`), stored as `sessions.jsonl` under `DOORMAN_DATA_DIR` (the `doorman_data` volume in docker, mounted `/data`), with a per-visit snapshot alongside.

New `.env` keys: `DOORMAN_UI_PORT` (default `8090`), `DOORMAN_UI_ENABLED` (default `true`), `DOORMAN_DATA_DIR` (default `/data`).

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
- `src/voice_local.py` - local engine: STT -> LLM -> TTS pipeline, endpointer, mic capture/stall recovery.
- `src/doorman_tools.py` - snapshot + notify tools, HA-native snapshot.
- `src/doorman_config.py` - config loading (env > profile files > defaults) + `.env` write/read for the UI.
- `src/transcripts.py` - conversation transcripts: in-memory current session + `sessions.jsonl` history + per-visit snapshot.
- `src/ui_api.py` - web UI backend (aiohttp): status/history/transcript/snapshot + config read/write + in-place restart.
- `src/ui_schema.py` - the config field registry (groups, types, hot-vs-restart) that drives the settings form.
- `src/ui/` - web UI frontend (vanilla JS, offline): `index.html`, `app.js`, `style.css`.
- `src/doorman_prompt.py` - household policy persona.
- `docker-compose.yml`, `Dockerfile` - container deployment.
- `tests/` - unit tests (run with `.venv/bin/python tests/<name>.py`).

## Notifications

The model can notify the homeowner via HA `notify.all_devices`. If a doorbell frame is attached, Doorman captures it with HA `camera.snapshot` and serves it at `http://<ha>/local/doorbell/<file>`.

## Testing / troubleshooting

- Door test: `docker compose logs -f doorman` then walk up or ring the bell; watch for `TRIGGER` and, per engine, the spoken-reply line: `[gemini said]` (Gemini) or `[visitor said]` / `[doorman said]` (local engine).
- Unit tests: run from the repo root, one at a time: `.venv/bin/python tests/test_animal_trigger.py` (all config, decision, prompt, and reaction unit tests). Same pattern for `tests/test_config.py`, `tests/test_snapshot.py`, etc.
- The UI/transcript/config-file tests are pytest-based: `.venv/bin/python -m pytest tests/test_ui_api.py tests/test_transcripts.py tests/test_env_file.py -q` (needs `pytest-aiohttp` for the API tests: `.venv/bin/pip install pytest-aiohttp`).
- Animal test: publish a synthetic Frigate cat event to the MQTT broker and watch for `TRIGGER`, `animal notify`, and `[gemini said]` lines:
  `mosquitto_pub -h <mqtt> -p 1883 -u <user> -P <pass> -t frigate/events -m '{"type":"new","after":{"id":"cat-test-1","camera":"front_doorbell","label":"cat"}}'`
  (Swap `label` to `dog` for a dog. Send a few times to confirm a different greeting line comes out each time.)
- go2rtc talkback notes and verified transport facts: `TRANSPORT.md`.

## Planned features

A living list of features being worked on, roughly ordered by priority and grouped by theme. The first group is the nearest-term.

### Voice & conversation

- [x] **Local voice engine** - a fully on-LAN voice pipeline (faster-whisper or Parakeet STT + local LLM brain + Chatterbox TTS) selectable via `DOORMAN_VOICE_ENGINE=local`. Gemini Live stays the default. Built and documented above in the "Local voice engine" section; `src/voice_local.py`.
- [ ] **Human takeover** - the owner interrupts the AI mid-session and talks to the visitor directly through the doorbell camera; the AI is put on hold and can hand the conversation back.
- [ ] **Barge-in / streaming STT** - let the visitor interrupt a long AI reply, replacing the half-duplex turn loop with streaming recognition to cut per-turn latency.

### People & policy

- [ ] **Silent note for ignored faces** - an optional HA notification so a fully-ignored face still leaves a record without a spoken greeting.
- [ ] **Pre-recognition ignore gate** - skip an ignored face the moment it is detected instead of after the recognition wait.
- [ ] **Door lock** - recognized face + "open the door" intent + you're home unlocks the entry lock.
- [ ] **Delivery / package mode** - detect a package, frame it in the notification, and offer an optional "leave it with me" flow.

### Ops & visibility

- [x] **Web UI / control panel** - manage all settings in the browser instead of editing `.env`, with an interaction history (per-visit transcript + snapshot + recognized name) and a status/health view. **Done** - see the "Web UI" section above.
- [ ] **HA watchdog + down alert** - a health entity that alerts the moment Doorman is wedged.
- [ ] **Local recording of interactions** - privacy-gated, off by default.
- [ ] **Listening status entity** - an HA entity that exposes whether Doorman is in an active conversation right now.

## License

MIT, see `LICENSE`.
