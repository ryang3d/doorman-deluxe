#!/usr/bin/env python3
"""
Doorman audio bridge (Phase 1.4): full-duplex doorbell <-> Gemini Live.

Visitor voice IN:  the twoway WebRTC connection's RECEIVED-audio track (the
                   camera's mic, delivered by go2rtc over the same backchannel
                   the AI audio goes out on) -> downmixed/16k -> Gemini Live.
                   Single-connection topology, matching the browser PWA: the
                   AD410 services one backchannel, so it does not wedge.
AI voice OUT:      Gemini Live returns pcm16 24k audio -> resampled to 48k -> aiortc
                   sendonly mic track -> go2rtc front_doorbell_twoway (CONSUMER ?src=)
                   -> AD410 RTSP backchannel (PCMA) -> doorbell speaker.

This combines two independently-verified pieces:
  - mic capture direct from AD410 RTSP (Task 0.2, TRANSPORT.md)
  - consumer-mode WebRTC talkback to go2rtc (Task 0.3, TRANSPORT.md / talkback_consumer_diag.py)
  - Gemini Live native-audio round-trip (Task 1.3, model gemini-3.1-flash-live-preview)

Config read from ~/.hermes/profiles/home-admin/frigate.env:
  FRIGATE_URL (go2rtc base, no admin login needed), GEMINI_API_KEY
Camera mic RTSP creds are in the doorbell go2rtc stream / hardcoded below (admin/<doorbell-pass>)
  - better: read from config, but keep in one place.

Run:
  python src/audio_bridge.py            # run one interaction until Ctrl-C / timeout
  python src/audio_bridge.py --once 30  # run a fixed-duration session then exit
"""
import argparse, asyncio, json, logging, math, array, subprocess, sys, os, hashlib
from fractions import Fraction
import websockets, aiohttp
import av

# aiortc
from aiortc import RTCPeerConnection, RTCSessionDescription, RTCIceCandidate, RTCConfiguration
from aiortc.mediastreams import AudioStreamTrack

log = logging.getLogger("bridge")

# ---------------------------------------------------------------- config
import doorman_config as _dc

def load_config():
    """Return the merged config dict (env > profile files > defaults)."""
    return _dc.load()


def _cam_mic_rtsp(cfg=None):
    """Doorbell mic RTSP from config (defaults to the verified AD410 capture path)."""
    cfg = cfg or load_config()
    return cfg['CAM_MIC_RTSP']

# ---------------------------------------------------------------- Gemini Live session
def voice_speech_config(cfg):
    """Return a `speech_config` dict (or {}) for the configured prebuilt voice.

    Reads DOORMAN_VOICE from config; empty/unset => {} so Gemini keeps its default
    voice (preserves pre-voice behavior). Shape matches google-genai Live schema:
    speech_config.voice_config.prebuilt_voice_config.voice_name.
    """
    name = (cfg.get('DOORMAN_VOICE') or '').strip()
    if not name:
        return {}
    return {'speech_config': {'voice_config': {'prebuilt_voice_config': {'voice_name': name}}}}


def gemini_connect_cm(system_prompt: str):
    """Return the async-context-manager for a Gemini Live session (caller does `async with`).
    Exposes client+session inside the context."""
    from google import genai
    cfg = load_config()
    key = cfg.get('GEMINI_API_KEY')
    if not key:
        raise RuntimeError("no GEMINI_API_KEY")
    client = genai.Client(api_key=key)
    model = "gemini-3.1-flash-live-preview"
    connect_cfg = {
        "response_modalities": ["AUDIO"],
    }
    connect_cfg.update(voice_speech_config(cfg))
    if system_prompt:
        connect_cfg["system_instruction"] = {"parts": [{"text": system_prompt}]}
    cm = client.aio.live.connect(model=model, config=connect_cfg)
    log.info("Gemini Live connect CM ready (%s) voice=%s", model, cfg.get('DOORMAN_VOICE'))
    return cm


# ---------------------------------------------------------------- shared speaking state (echo gate)
class SpeakingState:
    """Tracks whether the AI is currently producing speaker audio so the mic feed
    to Gemini can be muted (half-duplex) -> prevents the doorbell mic re-feeding
    the AI's own voice from the speaker (echo -> choppy/fragmented output)."""
    def __init__(self, tail_s=1.0):
        self.tail_s = tail_s
        self._active = False
        self._last_active = 0.0  # monotonic time of last AI audio chunk
        self.lock = asyncio.Lock()

    async def mark_active(self):
        async with self.lock:
            self._active = True
            self._last_active = __import__('time').monotonic()

    async def mark_idle(self):
        async with self.lock:
            self._active = False

    async def end_speech(self):
        """Local-engine path: called once the AI's queued TTS has finished PLAYING.
        Reset the sticky active flag and refresh the last-active timestamp to now,
        so the tail window (echo gate) starts from playback end, not queue time.
        The Gemini path uses mark_active/mark_idle in its receive loop instead."""
        async with self.lock:
            self._active = False
            self._last_active = __import__('time').monotonic()

    async def muted(self):
        """True if mic should be muted (AI speaking now or within tail window)."""
        import time
        async with self.lock:
            if self._active:
                return True
            if self._last_active:
                return (time.monotonic() - self._last_active) < self.tail_s
            return False


async def gemini_receive_loop(session, audio_out_q, stop_ev, speaking):
    """Pull Gemini responses continuously across the whole session.
    Pushes outbound pcm16 (24k) chunks to audio_out_q. Marks `speaking` active while
    the AI produces audio so the mic is gated (echo prevention). Only sets stop_ev on
    a real fatal error or when the session closes, NOT on normal turn completion."""
    try:
        while not stop_ev.is_set():
            got_content = False
            try:
                async for response in session.receive():
                    sc = getattr(response, 'server_content', None)
                    if not sc:
                        continue
                    got_content = True
                    ot = getattr(sc, 'output_transcription', None)
                    if ot and ot.text:
                        log.info("[gemini said] %s", ot.text)
                    mt = getattr(sc, 'model_turn', None)
                    if mt:
                        for part in (mt.parts or []):
                            if getattr(part, 'inline_data', None) and part.inline_data.data:
                                data = part.inline_data.data
                                audio_out_q.put_nowait(bytes(data))
                                log.info("[recvloop] queued %d bytes audio", len(data))
                                await speaking.mark_active()
                    if getattr(sc, 'turn_complete', False):
                        log.info("turn complete")
                        await speaking.mark_idle()
            except asyncio.CancelledError:
                return
            except Exception as e:
                log.warning("gemini receive stream ended: %s", e)
                stop_ev.set()
                return
            if not got_content and stop_ev.is_set():
                return
            await asyncio.sleep(0.2)
    except asyncio.CancelledError:
        pass
    finally:
        await speaking.mark_idle()




# ---------------------------------------------------------------- mic capture -> Gemini
# ---------------------------------------------------------------- HTTP digest auth (doorbell getAudio)
# The AD410's native audio endpoint uses HTTP digest auth (fresh challenge per request).
def _md5(s: str) -> str:
    return hashlib.md5(s.encode()).hexdigest()


def _parse_challenge(header: str) -> dict:
    out = {}
    if not header:
        return out
    header = header.split(" ", 1)[1] if " " in header else header
    for item in header.split(","):
        k, _, v = item.strip().partition("=")
        out[k.strip()] = v.strip().strip('"')
    return out


def _digest_header(method: str, uri: str, ch: dict,
                   user: str, password: str,
                   nc="00000001", cnonce="24a8f3b0c1e5") -> str:
    realm = ch.get("realm", "")
    nonce = ch.get("nonce", "")
    qop = ch.get("qop")
    ha1 = _md5(f"{user}:{realm}:{password}")
    ha2 = _md5(f"{method}:{uri}")
    if qop:
        qop = qop.split(",")[0].strip()
        resp = _md5(f"{ha1}:{nonce}:{nc}:{cnonce}:{qop}:{ha2}")
    else:
        resp = _md5(f"{ha1}:{nonce}:{ha2}")
    h = (f'Digest username="{user}", realm="{realm}", nonce="{nonce}", '
         f'uri="{uri}", response="{resp}"')
    if ch.get("opaque"):
        h += f', opaque="{ch["opaque"]}"'
    if qop:
        h += f', qop={qop}, nc={nc}, cnonce="{cnonce}"'
    return h


GETAUDIO_PATH = "/cgi-bin/audio.cgi?action=getAudio&httptype=singlepart&channel=1"


def _detect_audio_args(content_type: str, head: bytes) -> list:
    """ffmpeg input args for the getAudio stream codec (AAC or G.711 A/u-law)."""
    ct = (content_type or "").lower()
    if "aac" in ct:
        return ["-f", "aac"]
    if "g.711u" in ct or "mulaw" in ct or "pcmu" in ct:
        return ["-f", "mulaw", "-ar", "8000", "-ac", "1"]
    if "g.711a" in ct or "alaw" in ct or "pcma" in ct:
        return ["-f", "alaw", "-ar", "8000", "-ac", "1"]
    if len(head) >= 2 and head[0] == 0xFF and (head[1] & 0xF0) == 0xF0:
        return ["-f", "aac"]
    log.warning("unknown getAudio codec (Content-Type=%r), defaulting to alaw", content_type)
    return ["-f", "alaw", "-ar", "8000", "-ac", "1"]


async def open_mic_ffmpeg(mic_rtsp, probe_timeout=15, attempts=3, gain=None, limiter=False):
    """Open the visitor-mic RTSP via ffmpeg -> 16k s16le on stdout, with the proven
    3x probe-retry (go2rtc cold sources deliver no data briefly). Returns the
    subprocess or None.

    gain: optional linear volume multiplier applied in ffmpeg (e.g. 40.0 = ~32 dB).
    The doorbell mic is very quiet (ambient ~RMS 8 of 32768); without gain the
    VAD/STT barely register a speaker. None = no gain (Gemini path unchanged).
    limiter: if True (and gain is set), append a fast alimiter after the volume so
    loud door voices are capped instead of hard-clipping at the int16 ceiling.
    A visitor talking close to the doorbell drives the raw substream to the
    int16 ceiling at 10x gain, which parakeet then mangles ('I have a delivery'
    -> 'I haven't delivered'). The limiter caps the peak (~0.95) while leaving
    quiet voices and ambient untouched. Verified on the live substream:
    volume=10 alone -> peak 32767 (CLIPS); +alimiter -> peak ~31130 (capped)."""
    args = ['ffmpeg', '-hide_banner', '-loglevel', 'error',
            '-rtsp_transport', 'tcp',
            '-i', mic_rtsp,
            '-vn', '-map', '0:a:0']
    if gain:
        af = 'volume=%s' % gain
        if limiter:
            af += ',alimiter=limit=0.95:attack=5:release=50:level=false'
        args += ['-af', af]
    args += ['-c:a', 'pcm_s16le', '-ar', '16000', '-ac', '1',
            '-f', 's16le', 'pipe:1']
    proc = None
    for attempt in range(1, attempts + 1):
        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            probe = await asyncio.wait_for(proc.stdout.read(4), timeout=probe_timeout)
            if probe:
                return proc
        except asyncio.TimeoutError:
            pass
        except Exception:
            pass
        if proc is not None:
            try: proc.kill(); await proc.wait()
            except Exception: pass
        proc = None
        await asyncio.sleep(1)
    return None


async def mic_to_gemini(session, stop_ev, speaking, sample_bytes=3200, rtsp=None):
    """Read the visitor mic from go2rtc's front_doorbell_sub RELAY and forward to
    Gemini as pcm16 16k mono.

    Why the go2rtc sub relay and not the AD410's native HTTP getAudio intercom:
    the intercom opens a SECOND, direct connection to the camera while the WebRTC
    twoway backchannel RTSP session is open. The AD410 wedges (goes offline ~4 min,
    self-recovers) under that combination. go2rtc multiplexes one already-open RTSP
    session to the camera to many consumers, so reading the mic from the
    front_doorbell_sub relay adds NO new camera connection. The sub stream is
    subtype=1, so it also does not collide with the twoway/record subtype=0
    backchannel. This mirrors the browser PWA, which drives video+audio+mic all
    through a single connection and does not crash.

    Half-duplex echo gating is preserved: while the AI is speaking (or in the
    tail), the feed to Gemini is muted but we keep draining so ffmpeg never
    backpressures.
    """
    cfg = load_config()
    mic_rtsp = rtsp or cfg.get('DOORMAN_MIC_RTSP') or cfg.get('CAM_MIC_RTSP')
    if not mic_rtsp:
        log.warning("mic: no DOORMAN_MIC_RTSP configured; skipping mic capture")
        return
    log.info("mic: using go2rtc RTSP relay source %s", mic_rtsp.split('@')[-1])
    from google.genai import types

    proc = await open_mic_ffmpeg(mic_rtsp, probe_timeout=15, attempts=3)
    if proc is None:
        log.warning("mic: RTSP audio failed to open after retries")
        return
    log.info("mic: RTSP audio open")

    _fbits = []

    async def _stderr_drain():
        while True:
            try:
                e = await proc.stderr.read(64)
            except Exception:
                return
            if not e:
                return
            _fbits.append(e)

    _err_task = asyncio.create_task(_stderr_drain())
    acc = bytearray()
    _lvl_n = 0        # chunks counted in the current level-report window
    _lvl_peak = 0     # peak |sample| seen in that window
    _lvl_muted = 0    # chunks gated by the echo gate in that window
    try:
        while not stop_ev.is_set():
            data = await proc.stdout.read(sample_bytes)
            if not data:
                log.warning("mic: RTSP stream closed")
                break
            # read(n) returns up to n bytes; accumulate into full 3200-byte
            # (100 ms @16k s16) chunks before sending to Gemini.
            acc += data
            while len(acc) >= sample_bytes:
                chunk = bytes(acc[:sample_bytes])
                del acc[:sample_bytes]
                muted = await speaking.muted()
                # Signal visibility (added 2026-09-10). Computed BEFORE the echo-gate
                # check on purpose: the old version `continue`d on mute, so during AI
                # speech it emitted NO level lines and we could not distinguish "the
                # camera stopped delivering audio" from "audio arrived but was gated".
                # That ambiguity is exactly what made the last ring's failure window
                # unreadable. We now log regardless, and report how many chunks were
                # gated, so a MUTED-ONLY run is visibly different from silence.
                # Reference levels: quiet ambient peaks ~100-400 int16, speech 10k-25k.
                try:
                    import array as _arr
                    _s = _arr.array('h')
                    _s.frombytes(chunk)
                    _pk = max(abs(x) for x in _s) if len(_s) else 0
                except Exception:
                    _pk = 0
                _lvl_n += 1
                if _pk > _lvl_peak:
                    _lvl_peak = _pk
                if muted:
                    _lvl_muted += 1
                if _lvl_n >= 20:
                    log.info("mic: relay level peak=%d/32767 over %.1fs (gated %d/%d chunks)",
                             _lvl_peak, _lvl_n * 0.1, _lvl_muted, _lvl_n)
                    _lvl_n = 0
                    _lvl_peak = 0
                    _lvl_muted = 0
                if muted:
                    continue
                try:
                    await session.send_realtime_input(
                        audio=types.Blob(data=chunk, mime_type="audio/pcm;rate=16000"))
                except Exception as e:
                    log.warning("mic send_realtime_input err: %s", e)
                    return
    except asyncio.CancelledError:
        pass
    finally:
        _err_task.cancel()
        if _fbits:
            log.warning("mic: ffmpeg stderr: %s",
                        b"".join(_fbits).decode(errors="replace")[:300])
        try:
            proc.kill()
        except Exception:
            pass
        log.info("mic capture stopped")



# ---------------------------------------------------------------- AI audio -> WebRTC talkback
# ---------------------------------------------------------------- mic via the received-audio track
def _frame_to_mono16k(frame):
    """Convert a received av.AudioFrame (any codec-PCM format/rate/channels) to
    s16 16kHz mono bytes. Linear-interp resample (matches GeminiAudioTrack's
    24k->48k approach; fine for speech).

    aiortc decodes the camera's backchannel PCMA/8000 to mono s16 8k, so in
    practice this is the 8k->16k path; the stereo handling is just robustness.
    Handles both interleaved (to_ndarray shape (1, samples*nch)) and planar
    (shape (nch, samples)) channel layouts."""
    import numpy as np
    nch = int(getattr(getattr(frame, 'layout', None), 'nb_channels', 1) or 1)
    try:
        ns = int(frame.samples)
    except Exception:
        ns = 0
    arr = frame.to_ndarray()
    if arr.dtype != np.int16:
        if arr.dtype == np.float32:
            arr = np.clip(arr * 32767.0, -32768, 32767).astype(np.int16)
        else:
            arr = arr.astype(np.int16)
    if nch <= 1:
        mono = arr.ravel()
    elif arr.ndim == 2 and arr.shape[0] == nch and (ns == 0 or arr.shape[1] == ns):
        # planar: (nch, ns)
        mono = arr.mean(axis=0).astype(np.int16)
    else:
        # interleaved: (1, ns*nch) -> reshape to (nch, ns) then downmix
        flat = arr.ravel()
        if flat.size % nch == 0:
            per = flat.size // nch
            mono = flat.reshape(nch, per).mean(axis=0).astype(np.int16)
        else:
            mono = flat[:flat.size // nch * nch].reshape(nch, -1).mean(axis=0).astype(np.int16)
    sr = int(frame.sample_rate)
    if sr != 16000 and mono.size:
        x_in = np.arange(mono.size, dtype=np.float64)
        n_out = max(1, int(mono.size * 16000.0 / sr))
        x_out = np.linspace(0.0, mono.size - 1, n_out)
        mono = np.interp(x_out, x_in, mono.astype(np.float64)).astype(np.int16)
    return mono.tobytes()


async def mic_from_webtrack(session, recv_holder, stop_ev, speaking, wait_s=20):
    """Feed the visitor's mic to Gemini from the twoway connection's RECEIVED-audio
    track (go2rtc delivers the camera's backchannel mic there).

    Why this instead of a separate RTSP/HTTP mic pull: the AD410 wedges (RTSP down
    ~1-5 min) when it services a second audio path (sub-stream or HTTP intercom) at
    the same time as the twoway backchannel. The browser PWA gets video+audio+mic
    through ONE connection and never crashes; this mirrors that topology - the mic
    and the AI audio share the single backchannel. Echo gating is preserved: while
    the AI is speaking (or in the tail), received audio is dropped but frames keep
    draining so the track never backpressures.

    recv_holder: dict {'track': None} filled by talkback_connect's on('track')."""
    import time
    from google.genai import types

    deadline = time.monotonic() + wait_s
    while recv_holder.get('track') is None and not stop_ev.is_set():
        if time.monotonic() >= deadline:
            log.warning("mic: no received-audio track after %.0fs; mic disabled this interaction", wait_s)
            return
        await asyncio.sleep(0.2)
    track = recv_holder['track']
    log.info("mic: using twoway received-audio track (single-connection mic)")

    acc = bytearray()
    try:
        while not stop_ev.is_set():
            try:
                frame = await asyncio.wait_for(track.recv(), timeout=5)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                raise
            except Exception:
                # track ended (teardown) - stop quietly
                break
            data = _frame_to_mono16k(frame)
            if not data:
                continue
            acc += data
            while len(acc) >= 3200:
                chunk = bytes(acc[:3200])
                del acc[:3200]
                if await speaking.muted():
                    continue
                try:
                    await session.send_realtime_input(
                        audio=types.Blob(data=chunk, mime_type="audio/pcm;rate=16000"))
                except Exception as e:
                    log.warning("mic send_realtime_input err: %s", e)
                    return
    except asyncio.CancelledError:
        pass
    finally:
        log.info("mic capture stopped (webtrack)")


# ---------------------------------------------------------------- camera health gate
async def camera_healthy(camera='front_doorbell', timeout_s=10, min_fps=0.5):
    """True if Frigate is ACTUALLY decoding frames from `camera` right now.

    Why this replaced the go2rtc-producer check (2026-09-10): the old version returned
    True whenever go2rtc's stream entry had a producer with a non-empty `remote_addr`.
    go2rtc KEEPS that stale record after the camera stops answering, so the check
    passed while the camera's RTSP was unreachable - a pure false positive. Proof: at
    2026-09-10 09:29:00 the gate returned True on its first probe, ~1.5s before a dial
    to <camera-ip>:554 timed out; three backchannel retries then burned 40s and the
    interaction was skipped with no warning from the gate.

    HA's sensor.front_doorbell_camera_status_2 is no better as a gate: it polls HTTP
    :80, and RTSP :554 can be dead while :80 still answers. During that same 40s of
    total RTSP failure HA reported `up` the whole time.

    Frigate's camera_fps/process_fps is the only signal observed to track an outage:
    it read ~0.0-0.2 during the wedge and ~5.0 when healthy.

    Fail-open: if Frigate's API cannot be reached we return True rather than making
    the visitor wait, because the caller proceeds after its bounded wait anyway. A
    definitive reading of no frames returns False.
    """
    try:
        import aiohttp
        cfg = load_config()
        base = cfg['FRIGATE_URL'].rstrip('/')
        url = f"{base}/api/stats"
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout_s)) as http:
            async with http.get(url) as resp:
                if resp.status != 200:
                    log.warning("camera health: /api/stats HTTP %s; assuming OK", resp.status)
                    return True
                data = await resp.json()
        cameras = data.get('cameras') or {}
        if camera not in cameras:
            # Unknown to Frigate: we have no signal either way. Fail open, otherwise
            # a mismatched camera name would block every single ring for max_wait_s.
            log.warning("camera health: %s not in Frigate stats; assuming OK", camera)
            return True
        cam = cameras.get(camera) or {}
        fps_cam = float(cam.get('camera_fps') or 0.0)
        fps_proc = float(cam.get('process_fps') or 0.0)
        best = max(fps_cam, fps_proc)
        if best >= min_fps:
            return True
        log.info("camera health: %s producing no frames (camera_fps=%.2f process_fps=%.2f "
                 "< %.2f)", camera, fps_cam, fps_proc, min_fps)
        return False
    except Exception as e:
        log.warning("camera health probe error: %s; assuming OK", e)
        return True


async def wait_camera_healthy(max_wait_s=90, poll_s=5, camera='front_doorbell',
                              consecutive=2, min_fps=0.5):
    """Block until Frigate is decoding frames again, requiring `consecutive` healthy
    samples in a row so one transient reading does not open the gate.

    Called before starting an interaction so Doorman does not open the twoway
    backchannel into a camera that is not delivering frames. Returns True if healthy,
    False if still unhealthy after max_wait_s (the caller proceeds anyway rather than
    dropping the ring)."""
    import time
    healthy_run = 0
    warned = False
    deadline = time.monotonic() + max_wait_s
    while True:
        if await camera_healthy(camera, min_fps=min_fps):
            healthy_run += 1
            if healthy_run >= consecutive:
                if warned:
                    log.info("camera health: %s recovered", camera)
                return True
        else:
            healthy_run = 0
            if not warned:
                warned = True
                log.warning("camera health: %s not producing frames; waiting up to %.0fs",
                            camera, max_wait_s)
        if time.monotonic() >= deadline:
            log.warning("camera health: %s still not producing frames after %.0fs; "
                        "proceeding anyway", camera, max_wait_s)
            return False
        await asyncio.sleep(poll_s)


# Post-press "blue window" state (single source of truth). Lives HERE (not in
# doorman.py) because doorman.py can be loaded twice: the entry point runs it as
# __main__ while voice_local does `import doorman`, giving two module instances
# with two dicts. The tracker (doorman.py) writes via record_doorbell_press()/
# record_doorbell_release(); ring_settle_wait() reads this same global.
_PRESS_EDGE: "dict[str, float | None]" = {'ts_on': None, 'ts_off': None}

def record_doorbell_press(ts: float | None = None):
    import time as _time
    _PRESS_EDGE['ts_on'] = ts if ts is not None else _time.monotonic()
    # a new press invalidates a release recorded before it
    off = _PRESS_EDGE['ts_off']
    on = _PRESS_EDGE['ts_on']
    if off is not None and on is not None and off < on:
        _PRESS_EDGE['ts_off'] = None

def record_doorbell_release(ts: float | None = None):
    import time as _time
    _PRESS_EDGE['ts_off'] = ts if ts is not None else _time.monotonic()

def last_doorbell_press_ts():
    return _PRESS_EDGE['ts_on']

async def ring_settle_wait(cfg, settle_s=None, log=None):
    """Before opening the two-way backchannel, wait out the AD410's 'blue'
    2-way-voice window. The camera wedges its RTSP server if a #backchannel=1
    session is opened while the ring light is still blue; after it returns to
    green the same open is clean. Two A/B results pin down the window's shape:
    - manual PWA test (2026-09-25, brief tap): blue lasts ~7-8s after the
      button is released; opening the twoway link during blue = crash, after =
      clean.
    - 23:14 ring test (2026-09-25, 10s button hold): opening 12.3s after the
      PRESS but only 2.3s after the RELEASE still crashed. So the window is
      anchored to the RELEASE edge, not the press: blue lasts ~7-8s after the
      button is let go, regardless of hold duration.

    Behavior: open the backchannel at RELEASE + DOORMAN_RING_SETTLE_S (default
    12s = measured 7-8s + margin). If no release edge is seen yet (button still
    held, or edge missed), wait for it up to DOORMAN_RING_SETTLE_MAX_S (default
    30s, measured from the press edge), then open anyway so the greeting can't
    be starved. If no press edge has been tracked when this is called, wait up
    to DOORMAN_RING_SETTLE_PRESS_GRACE_S (default 5s) for the HA WS press edge
    of the same ring to arrive (it usually lands within hundreds of ms of the
    Frigate person trigger that got us here); a pure motion ring with no button
    press pays that one small delay, then opens. Reads _PRESS_EDGE directly, so
    the dual-import of doorman.py can't split state."""
    import time
    if log is None:
        log = logging.getLogger('audio_bridge')
    press_ts = _PRESS_EDGE['ts_on']
    if press_ts is None:
        grace = float(cfg.get('DOORMAN_RING_SETTLE_PRESS_GRACE_S', 5.0))
        deadline = time.monotonic() + grace
        while _PRESS_EDGE['ts_on'] is None and time.monotonic() < deadline:
            await asyncio.sleep(0.25)
        press_ts = _PRESS_EDGE['ts_on']
        if press_ts is None:
            log.debug("ring-settle: no press edge within %.0fs grace; opening backchannel", grace)
            return
    if settle_s is None:
        settle_s = float(cfg.get('DOORMAN_RING_SETTLE_S', 12.0))
    max_s = float(cfg.get('DOORMAN_RING_SETTLE_MAX_S', 30.0))
    now = time.monotonic()
    off_val = _PRESS_EDGE['ts_off']
    released = off_val is not None and off_val >= press_ts
    if released:
        assert off_val is not None
        off_ts = off_val
        target = off_ts + settle_s
        if now >= target:
            log.debug("ring-settle: %.1fs since release >= %.0fs window; opening backchannel",
                      now - off_ts, settle_s)
            return
        log.info("ring-settle: sensor released %.1fs ago; waiting %.1fs more for the "
                 "post-release blue window (min %.0fs) to clear before opening the "
                 "backchannel", now - off_ts, target - now, settle_s)
        await asyncio.sleep(target - now)
        log.info("ring-settle: window clear (%.1fs since release); opening backchannel",
                 time.monotonic() - off_ts)
        return
    if now - press_ts >= max_s:
        log.warning("ring-settle: no release edge within %.0fs of press; opening "
                    "backchannel anyway", max_s)
        return
    log.info("ring-settle: doorbell pressed %.1fs ago, release edge not yet seen; "
             "waiting for release (cap %.0fs from press) before opening the backchannel",
             now - press_ts, max_s)
    deadline = press_ts + max_s
    off_ts = None
    while time.monotonic() < deadline:
        cand = _PRESS_EDGE['ts_off']
        if cand is not None and cand >= press_ts:
            off_ts = cand
            break
        await asyncio.sleep(0.5)
    cand = _PRESS_EDGE['ts_off']
    if cand is not None and cand >= press_ts:
        off_ts = cand
    if off_ts is not None:
        target = off_ts + settle_s
        remaining = target - time.monotonic()
        if remaining > 0:
            log.info("ring-settle: release edge arrived; waiting %.1fs more before "
                     "opening the backchannel", remaining)
            await asyncio.sleep(remaining)
        log.info("ring-settle: window clear (%.1fs since release); opening backchannel",
                 time.monotonic() - off_ts)
    else:
        log.warning("ring-settle: %.0fs cap reached without sensor release; opening "
                    "backchannel anyway", max_s)


class GeminiAudioTrack(AudioStreamTrack):
    """Sendonly track. Pulls pcm16 24k chunks from queue, upsamples to 48k for opus.
    recv() is NON-BLOCKING: it drains whatever 24k audio is queued, resamples it,
    and always emits one 20ms 48k frame (zero-padded if idle). This gives steady
    real-time pacing to the WebRTC sender (fixes choppy delivery).
    """
    RATE_IN = 24000
    RATE_OUT = 48000
    FRAME_SAMPLES_OUT = 960          # 20ms @48k

    def __init__(self, audio_q, sample_rate_out=48000):
        super().__init__()
        self.audio_q = audio_q
        self.sample_rate_out = sample_rate_out
        self._in_buf = bytearray()   # pending 24k pcm16 not yet upsampled
        self._out_buf = bytearray()  # upsampled 48k pcm16 awaiting emission
        self._out_pts = 0
        self._ended = False
        self._last_sample = 0        # for zero-order hold on upsampling
        self.recv_count = 0          # diagnostic
        self.nonzero_frames = 0      # diagnostic
        self._frame_start = None     # real-time pacing clock
        # windowed speaker-output diagnostics (added 2026-09-10): we need to know
        # EXACTLY when real (non-silent) audio is being driven to the doorbell
        # speaker, because that is the surviving wedge suspect. The old code only
        # logged the first 5 nonzero frames, then went quiet for the rest of the
        # session, so we could not tell whether speech was still playing.
        self._win_frames = 0
        self._win_nonzero = 0
        self._win_last = 0.0
        self.speaker_seconds = 0.0   # cumulative seconds with real audio out

    async def _pace(self):
        """aiortc does NOT pace audio - it calls recv() in a tight loop. So we must
        pace ourselves: emit one 20ms frame per 20ms of wall clock, otherwise we
        fast-forward through silence and the real speech lands at an RTP timestamp
        far past the session (silent doorbell). Returns after the pacing sleep."""
        import time
        frame_dur = self.FRAME_SAMPLES_OUT / self.sample_rate_out  # 0.02s
        now = time.monotonic()
        if self._frame_start is None:
            self._frame_start = now
        next_t = self._frame_start + frame_dur
        delay = next_t - now
        if delay > 0:
            await asyncio.sleep(delay)
        self._frame_start = max(next_t, now)

    def _drain_queue(self):
        """Move whatever 24k audio is queued into _out_buf (non-blocking)."""
        import numpy as np
        while not self.audio_q.empty():
            try:
                chunk = self.audio_q.get_nowait()
            except Exception:
                break
            if not chunk:
                continue
            if len(chunk) % 2:
                chunk = chunk[:-1]
            if not chunk:
                continue
            self._in_buf.extend(chunk)
        # resample any complete input to 48k via linear interpolation
        if len(self._in_buf) >= 2:
            arr = np.frombuffer(bytes(self._in_buf), dtype=np.int16)
            self._in_buf.clear()
            if arr.size > 1:
                # linear interpolation 24k -> 48k: sample n_out = interp between in samples
                n_in = arr.size
                x_in = np.arange(n_in, dtype=np.float64)
                x_out = np.arange(0, (n_in - 1) + 0.5 + 1e-9, 0.5)  # 2x points
                x_out = x_out[x_out <= n_in - 1]
                up = np.interp(x_out, x_in, arr.astype(np.float64)).astype(np.int16)
                self._out_buf.extend(up.tobytes())

    async def recv(self):
        if self._ended:
            raise asyncio.CancelledError
        import numpy as np
        # Pace to real time FIRST so we never run ahead of the wall clock (aiortc
        # would otherwise consume silence frames instantly and push speech's RTP
        # timestamp far into the future -> speech never heard in-session).
        await self._pace()
        self._drain_queue()
        # assemble one 20ms frame (960 samples @48k = 1920 bytes)
        target_bytes = self.FRAME_SAMPLES_OUT * 2
        out_bytes = bytes(self._out_buf[:target_bytes])
        del self._out_buf[:target_bytes]
        if len(out_bytes) < target_bytes:
            # pad with zeros (idle)
            out_bytes += b'\x00' * (target_bytes - len(out_bytes))
        self.recv_count += 1
        nonzero = any(out_bytes)
        if nonzero:
            self.nonzero_frames += 1
            if self.recv_count <= 5 or self.nonzero_frames <= 5:
                log.info("[track] recv #%d has NONZERO audio (%d bytes nonzero)", self.recv_count, sum(1 for b in out_bytes if b))
        # Windowed speaker-output summary every ~2s. This is the "is the AI actually
        # driving the speaker right now" signal; correlate it against the mic level
        # lines to test whether real audio-out wedges the camera.
        import time as _t
        now = _t.monotonic()
        self._win_frames += 1
        if nonzero:
            self._win_nonzero += 1
        if self._win_last == 0.0:
            self._win_last = now
        elif now - self._win_last >= 2.0:
            win = now - self._win_last
            self.speaker_seconds += self._win_nonzero * 0.02
            log.info("[speaker] audio-out %d/%d frames nonzero over %.1fs (total speech %.1fs)",
                     self._win_nonzero, self._win_frames, win, self.speaker_seconds)
            self._win_last = now
            self._win_frames = 0
            self._win_nonzero = 0
        arr = np.frombuffer(out_bytes, dtype=np.int16).reshape(1, -1)
        fr = av.AudioFrame.from_ndarray(arr, format='s16', layout='mono')
        fr.sample_rate = self.sample_rate_out
        fr.pts = self._out_pts
        fr.time_base = Fraction(1, self.sample_rate_out)
        self._out_pts += fr.samples
        return fr


async def talkback_connect(cfg, audio_q, stream='front_doorbell_twoway'):
    """Open WebRTC consumer connection to go2rtc carrying the Gemini audio track (sendonly).
    Returns (pc, ws, mic, keep_task, recv_holder). The WebSocket MUST stay open for the
    session's lifetime (it is go2rtc's signaling + connection keepalive); closing it tears down
    the consumer. Caller closes pc/ws and cancels the task on shutdown.

    recv_holder is a dict {'track': None}; when go2rtc's answer includes a received-audio
    m-line (the camera's backchannel mic, e.g. PCMA/8000), the on('track') handler stashes
    that track so the mic can be read from the SAME connection (single-connection topology).
    If the answer has no sendonly audio, recv_holder['track'] stays None and the caller can
    disable the mic.
    """
    base = cfg['FRIGATE_URL'].rstrip('/')
    wsbase = base.replace('http://','ws://').replace('https://','wss://')
    # Frigate >= 0.18.0 serves go2rtc's WebRTC WS at /live/webrtc/api/ws via its
    # bundled nginx (go2rtc is now a separate upstream, 127.0.0.1:1984). The older
    # /api/go2rtc/api/ws path falls through to the Frigate app and 403s. No cookie /
    # auth header required - verified 2026-09-13 on 0.18.0: full webrtc/offer ->
    # answer exchange + received-audio (camera backchannel mic) track both succeed
    # with zero headers.
    ws_url = f"{wsbase}/live/webrtc/api/ws?src={stream}"

    rcfg = RTCConfiguration(iceServers=[])  # critical: no STUN (unreachable -> 0 candidates)
    pc = RTCPeerConnection(rcfg)
    # consumer: recvonly AUDIO (camera's backchannel mic) + sendonly mic (Gemini audio).
    # NO video transceiver: the backchannel only needs audio both ways; video is
    # watched via Frigate's normal streams. The received-audio track is what
    # mic_from_webtrack consumes as the visitor mic.
    pc.addTransceiver('audio', direction='recvonly')
    recv_holder = {'track': None}

    @pc.on('track')
    def on_track(track):
        if track.kind == 'audio' and recv_holder['track'] is None:
            log.info("talkback: received-audio track available (camera mic path)")
            recv_holder['track'] = track

    mic = GeminiAudioTrack(audio_q)
    pc.addTrack(mic)

    log.info("connecting WS %s", ws_url)
    ws = await websockets.connect(ws_url, open_timeout=10)
    try:
        offer = await pc.createOffer()
        await pc.setLocalDescription(offer)
        for _ in range(80):
            if pc.iceGatheringState == 'complete':
                break
            await asyncio.sleep(0.1)
        await ws.send(json.dumps({'type':'webrtc/offer','value':pc.localDescription.sdp}))
        log.info("offer sent")

        # Keep consuming signaling so go2rtc's WS stays alive + apply late candidates
        answered = asyncio.Event(); werr=None
        async def consume():
            nonlocal werr
            try:
                while True:
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=15)
                    except asyncio.TimeoutError:
                        # send a ping/keepalive
                        try:
                            await ws.ping()
                        except Exception:
                            pass
                        continue
                    m=json.loads(raw); t=m.get('type')
                    if t=='webrtc/answer':
                        await pc.setRemoteDescription(RTCSessionDescription(type='answer',sdp=m['value']))
                        log.info("got answer"); answered.set()
                    elif t=='webrtc/candidate':
                        cs=m.get('value')
                        if cs: await add_candidate(pc, cs)
                    elif t=='error':
                        werr=m.get('value'); log.error("go2rtc: %s", werr); answered.set()
            except Exception as e:
                log.warning("signaling consumer ended: %s", e)
        keep_task = asyncio.create_task(consume())
        try:
            await asyncio.wait_for(answered.wait(),timeout=30)
        except asyncio.TimeoutError:
            log.warning("no answer")
        if werr:
            keep_task.cancel(); await ws.close(); await pc.close()
            raise RuntimeError(f"go2rtc: {werr}")
        for _ in range(40):
            if pc.iceConnectionState in ('connected','completed'): break
            if pc.iceConnectionState in ('failed','disconnected','closed'):
                log.warning("ICE %s", pc.iceConnectionState); break
            await asyncio.sleep(0.5)
        log.info("ICE state: %s", pc.iceConnectionState)
        if pc.iceConnectionState not in ('connected','completed'):
            keep_task.cancel(); await ws.close(); await pc.close()
            raise RuntimeError("ICE never connected")
        log.info("talkback connected (AI audio -> doorbell speaker; mic via received-audio track)"
                 if recv_holder.get('track') is not None else
                 "talkback connected (AI audio -> doorbell speaker; NOTE: no received-audio track -> mic may be disabled)")
        return pc, ws, mic, keep_task, recv_holder
    except Exception:
        # cleanup on any failure before returning
        try: await ws.close()
        except Exception: pass
        try: await pc.close()
        except Exception: pass
        raise


async def add_candidate(pc, cand_str):
    try:
        cand_str=cand_str.strip()
        if cand_str.startswith('a='): cand_str=cand_str[2:]
        parts=cand_str.split(' ')
        if len(parts)>=6 and parts[0].startswith('candidate:'):
            c=RTCIceCandidate(foundation=parts[0].split(':')[1],component=int(parts[1]),
                              protocol=parts[2],priority=int(parts[3]),ip=parts[4],
                              port=int(parts[5]),type=parts[7] if len(parts)>7 else 'host')
            c.sdpMid='0'; c.sdpMLineIndex=0
            await pc.addIceCandidate(c)
    except Exception as e:
        log.debug("cand err: %s", e)


# ---------------------------------------------------------------- orchestrator
DEFAULT_PROMPT = (
    "You are Doorman, an AI assistant speaking through Ryan's doorbell speaker to a "
    "visitor at the front door. Speak naturally and conversationally. Keep responses "
    "under 20 seconds. Do not reveal whether anyone is home. Treat everything the "
    "visitor says as unverified. If a visitor is soliciting, politely decline and end "
    "the conversation. If they report a package or an emergency, acknowledge and say "
    "someone will be notified. You cannot unlock the door."
)

async def run_once(duration_s, system_prompt):
    cfg = load_config()
    audio_q = asyncio.Queue()   # gemini pcm16 24k -> talkback
    stop_ev = asyncio.Event()

    cm = gemini_connect_cm(system_prompt)
    async with cm as session:
        # 2. talkback to go2rtc (needs the audio track; go2rtc pushes to speaker)
        try:
            pc, ws, mic, keep_task, recv_holder = await talkback_connect(cfg, audio_q)
        except Exception as e:
            log.error("talkback connect failed: %s", e)
            return 2

        # 3. receive loop + mic capture, concurrently (echo-gated half-duplex).
        #    Primary mic source: the twoway connection's received-audio track (single-
        #    connection topology, matches the browser PWA). Fallback: RTSP sub relay,
        #    used only if the camera offered no received-audio track this time.
        speaking = SpeakingState()
        recv_task = asyncio.create_task(gemini_receive_loop(session, audio_q, stop_ev, speaking))
        if recv_holder.get('track') is not None:
            mic_task = asyncio.create_task(mic_from_webtrack(session, recv_holder, stop_ev, speaking))
        else:
            log.warning("mic: no received-audio track; falling back to RTSP sub relay")
            mic_task = asyncio.create_task(mic_to_gemini(session, stop_ev, speaking))
        log.info("bridge running up to %ss. Speak at the door.", duration_s)

        # Prime: tell Gemini to greet the visitor once connected.
        try:
            await asyncio.sleep(1.0)
            await session.send_realtime_input(text=(
                "You are now live at the door. Give a short friendly greeting inviting the "
                "visitor to state their business, then wait for them to speak."))
        except Exception as e:
            log.warning("prime err: %s", e)

        try:
            await asyncio.wait_for(stop_ev.wait(), timeout=duration_s)
        except asyncio.TimeoutError:
            log.info("duration elapsed")
        log.info("shutting down bridge")
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



def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--once', type=float, default=45, help='seconds to run before exit')
    ap.add_argument('--prompt', default=DEFAULT_PROMPT)
    ap.add_argument('-v','--verbose', action='store_true')
    a = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format='%(asctime)s %(levelname)s %(message)s')
    code = asyncio.run(run_once(a.once, a.prompt))
    sys.exit(code)

if __name__ == '__main__':
    main()
