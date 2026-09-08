# TRANSPORT.md — Doorman Phase 0 verified transport facts (2026-09-08)

Status: Phase 0 COMPLETE (all 3 tasks VERIFIED 2026-09-08). Talkback audio-out to AD410 speaker CONFIRMED working in consumer mode by Ryan at the door.

## Verified: which go2rtc owns the doorbell streams (Task 0.1)

- go2rtc = Frigate's BUNDLED go2rtc 1.9.10, running on the Frigate host (<frigate-host>), Frigate 0.17.2.
- Frigate web/API: http://<frigate-host>:5001/
- go2rtc API prefix: http://<frigate-host>:5001/api/go2rtc/  (real JSON)
  - streams:  GET /api/go2rtc/api/streams
  - config:   GET /api/go2rtc/api/config  (YAML)
  - webrtc:   POST /api/go2rtc/api/webrtc
  - ws:       GET  /api/go2rtc/api/ws   (websocket signaling)
- The front_doorbell_twoway stream is declared on THIS go2rtc (Frigate's), NOT on the HA box. HA box go2rtc integration proxy paths all 404; HA pulls the stream from Frigate's go2rtc.
- the public tunnel hostname is Cloudflare-fronted (origin hidden); not needed for direct programmatic access.

## Doorbell stream definitions (confirmed, raw)

front_doorbell  -> rtsp://admin:PW@<camera-ip>:554/cam/realmonitor?channel=1&subtype=0   (main, 720x576 h264 15fps, PCMA/8000 audio)
front_doorbell_sub -> rtsp://...channel=1&subtype=1   (5fps, MPEG4/AAC 8000 audio)
front_doorbell_twoway -> [rtsp ...subtype=1, ffmpeg:front_doorbell_twoway#audio=opus]   <- the two-way stream (WebRTC-capable)
front_doorbell_twoway_fullres -> [rtsp ...subtype=0, ffmpeg:...fullres#audio=opus]

Camera creds (from configuration.yaml amcrest block): admin / <doorbell-pass>, host <camera-ip>.
Camera is DIRECTLY reachable from this host on RTSP :554 and HTTP :80 (ONVIF :8000 closed).

## Verified: mic audio capture (Task 0.2) — WORKS, no go2rtc needed

Direct RTSP to the AD410 carries the doorbell mic. Captured 12s to pcm_s16le 16k mono wav:
  ffmpeg -rtsp_transport tcp -i "rtsp://admin:PW@<camera-ip>:554/cam/realmonitor?channel=1&subtype=1" -t 12 -c:a pcm_s16le -ar 16000 -ac 1 out.wav
Result: 12.0s, 16kHz mono, mean_volume -36 dB, max -14 dB (real ambient signal, not silence).
Command verified working on this host (ffmpeg 6.1.1). Use subtype=1 (sub) or subtype=0 (main, PCMA).

## Verified: Frigate auth method (2026-09-08)

Frigate 0.17.2 auth is COOKIE-based, not body-token.
- Creds file: ~/.hermes/profiles/home-admin/frigate.env (mode 600 owner-only). Keys: FRIGATE_URL, FRIGATE_USER, FRIGATE_PASSWORD, FRIGATE_TOKEN (token optional/empty).
- Login:  POST /api/login  body {"user":"<user>","password":"<pw>"}  -> HTTP 200, empty body, sets cookie `frigate_token=<jwt>; HttpOnly; Path=/`. (422 if you send `username` key - must be `user`.)
- Use: send the cookie jar on every call:  curl -b /tmp/cookies.txt <url>
- Verified: /api/version -> 200 0.17.2-3d4dd3a ; /api/faces -> 200 {"Ryan":[file]} (so use -b cookie jar, not a Bearer header, for the Frigate API).
- go2rtc API prefix is under the same auth: http://<frigate-host>:5001/api/go2rtc/... (send the same cookie). Unauthenticated POST /api/go2rtc/api/webrtc = 403; retry WITH cookie.

## Face DB status (2026-09-08)
/api/faces (authed) -> {"Ryan":["Ryan_1788904848.404153.webp"]}. At least Ryan's face registered. Native large-ArcFace face rec enabled; DB seeding partially underway.

## Gemini Live API (Phase 1, verified 2026-09-08)
- API key: in ~/.hermes/profiles/home-admin/frigate.env as GEMINI_API_KEY (Ryan added it). Valid: 50 models accessible.
- Model string (VERIFIED working): `gemini-3.1-flash-live-preview`
- Config: {"response_modalities": ["AUDIO"]}
- Connect: `client.aio.live.connect(model=..., config=...)` from google.genai (SDK 2.22.0)
- Send audio: `session.send_realtime_input(audio=types.Blob(data=raw_pcm16_16k, mime_type="audio/pcm;rate=16000"))`
- Send text: `session.send_realtime_input(text="...")`
- Receive: `async for response in session.receive()`. Structure: response.server_content.model_turn.parts[].inline_data.data = outbound audio; server_content.output_transcription.text / input_transcription.text = transcripts.
- Native audio out is pcm16 24kHz mono (wrote as 24k wav successfully). ~2.5s of speech = ~120KB raw.
- VERIFIED: text prompt -> Gemini spoke "hello, this is the doorbell test" (out.wav, max -2.3dB). Test scripts: ~/doorman/tests/echo_live_text.py (deterministic) and echo_live.py (from a wav).

## CRITICAL architecture finding for talkback (Task 0.3) - CONFIRMED 2026-09-08

go2rtc's two-way audio to an RTSP/ONVIF camera (AD410 = ONVIF Profile T / Dahua-family) ONLY routes
audio to the camera speaker when the WebRTC client connects as a CONSUMER (?src=<stream>) and offers
a sendonly mic track, mirroring exactly what the browser PWA does. The camera RTSP source then
advertises a backchannel: `audio, sendonly, PCMA/8000`, and go2rtc feeds the client's audio to it.

CRITICAL FAILURE MODE: connecting as a pure PRODUCER (?dst=<stream>) makes go2rtc treat the client
audio as a new stream source (feeding Frigate/viewers), NOT as the microphone for the camera speaker.
Producer mode produces SILENCE at the door even with clean ICE/WebRTC. Do NOT use ?dst= for talkback.

Working headless client: consumer mode (?src=front_doorbell_twoway) with:
  - pc.addTransceiver('video', direction='recvonly')
  - pc.addTransceiver('audio', direction='recvonly')
  - pc.addTrack(sendonly mic AudioStreamTrack)   <- this carries the AI speech
Built + verified: ~/doorman/talkback_consumer_diag.py
CONFIRMED AUDIBLE by Ryan (2026-09-08): 1000Hz tone amp 0.7 heard at the door.

AD410 talk volume is ALREADY 100 (verified configManager table.VideoTalkPhoneGeneral.TalkVolume=100),
so volume is not a lever. The AD410 backchannel accepts PCMA/8000 (G.711 A-law); client offers
Opus/48k and go2rtc transcodes to PCMA (the front_doorbell_twoway stream's opus reproducer handles it).


## Open items for Task 0.3 (all RESOLVED 2026-09-08)
1. RESOLVED: go2rtc WebRTC signaling works through Frigate proxy (WS upgrades at /api/go2rtc/api/ws, no cookie needed). Protocol from source: send {type:'webrtc/offer',value:<sdp>}; go2rtc replies {type:'webrtc/answer',...} + trickle {type:'webrtc/candidate',value:<line>}; consume both concurrently.
2. RESOLVED: Talkback MUST be consumer mode (?src=), NOT producer (?dst=). Producer = silent. See "CRITICAL architecture finding" above.
3. RESOLVED: aiortc client needs RTCConfiguration(iceServers=[]) - default Google STUN unreachable from this host stalls gathering to 0 candidates. With empty iceServers, host candidates gather and ICE connects to go2rtc <frigate-host>:8555.
4. RESOLVED (Phase 0 exit gate): audible tone CONFIRMED at the AD410 speaker by Ryan 2026-09-08 via talkback_consumer_diag.py consumer mode.
5. ACTION: the consumer-mode diagnostic script (talkback_consumer_diag.py) is the proven base for the real Phase 1 voice bridge. It should be renamed/refactored into the actual bridge (replace the tone track with live mic/AI audio).

