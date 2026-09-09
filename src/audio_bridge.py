#!/usr/bin/env python3
"""
Doorman audio bridge (Phase 1.4): full-duplex doorbell <-> Gemini Live.

Visitor voice IN:  ffmpeg reads AD410 RTSP mic audio -> raw PCM 16k mono -> Gemini Live
                   (session.send_realtime_input audio=...)
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


async def mic_to_gemini(session, stop_ev, speaking, sample_bytes=3200, rtsp=None):
    """Read the visitor mic via the AD410's native HTTP audio endpoint and forward
    to Gemini as pcm16 16k mono. This replaces the old ffmpeg-RTSP pull.

    Why HTTP and not RTSP: pulling the mic over a SECOND RTSP session while the
    WebRTC talkback backchannel is open crashes the AD410 (concurrent sessions).
    The camera's HTTP getAudio endpoint opens no RTSP session, so the mic can run
    while we talk, exactly like the amcrest-intercom project does (proven on this
    camera). Half-duplex echo gating is preserved: while the AI is speaking (or in
    the tail), the feed to Gemini is muted but we keep draining so ffmpeg never
    backpressures.
    """
    import threading
    cfg = load_config()
    host = cfg.get('DOORMAN_DOORBELL_HOST', '<camera-ip>')
    user = cfg.get('DOORMAN_DOORBELL_USER', 'admin')
    password = cfg.get('DOORMAN_DOORBELL_PASSWORD', '')
    url = f"http://{host}{GETAUDIO_PATH}"
    # route anything the old signature passed (tests) into the doorbell host
    if rtsp:
        log.info("mic_to_gemini: rtsp arg ignored (HTTP getAudio in use)")

    async def _run_http_capture():
        async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=None, sock_connect=8)) as http:
            # fetch a fresh challenge, then authed GET
            async with http.get(url) as pre:
                ch = _parse_challenge(pre.headers.get('WWW-Authenticate', '')) if pre.status == 401 else {}
            headers = {}
            if ch:
                headers['Authorization'] = _digest_header('GET', GETAUDIO_PATH, ch, user=user, password=password)
            proc = None
            try:
                async with http.get(url, headers=headers) as resp:
                    if resp.status != 200:
                        log.warning("mic: getAudio HTTP %s", resp.status)
                        return
                    head = await resp.content.read(4)
                    in_args = _detect_audio_args(resp.headers.get('Content-Type'), head)
                    log.info("mic: getAudio open ct=%s -> ffmpeg %s",
                             resp.headers.get('Content-Type'), ' '.join(in_args))
                    proc = await asyncio.create_subprocess_exec(
                        'ffmpeg', '-hide_banner', '-loglevel', 'error',
                        *in_args, '-i', 'pipe:0',
                        '-f', 's16le', '-ar', '16000', '-ac', '1', 'pipe:1',
                        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE)
                    proc.stdin.write(head)
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

                    async def http_to_ffmpeg():
                        # Stream HTTP audio straight into ffmpeg stdin in chunks.
                        # Keep stdin open (live stream). Verified: -fflags nobuffer
                        # suppressed output on a live pipe; buffering to 16KB then
                        # draining also interfered. Plain per-chunk writes + drain
                        # produce continuous output on this camera.
                        try:
                            async for chunk in resp.content.iter_chunked(8192):
                                if proc.stdin.is_closing():
                                    break
                                proc.stdin.write(chunk)
                                await proc.stdin.drain()
                        except asyncio.CancelledError:
                            raise
                        except Exception as e:
                            log.info("mic: http->ffmpeg ended: %s", e)
                        finally:
                            try:
                                if not proc.stdin.is_closing():
                                    proc.stdin.close()
                            except Exception:
                                pass

                    from google.genai import types
                    pump = asyncio.create_task(http_to_ffmpeg())
                    acc = bytearray()
                    try:
                        while not stop_ev.is_set():
                            data = await proc.stdout.read(sample_bytes)
                            if not data:
                                log.warning("mic: ffmpeg stdout closed")
                                break
                            # read(n) returns UP TO n bytes; ffmpeg emits 16k-s16 in
                            # ~1600-byte pieces, so accumulate partial reads into
                            # full 3200-byte (100 ms) chunks before sending to Gemini.
                            acc += data
                            while len(acc) >= sample_bytes:
                                chunk = bytes(acc[:sample_bytes])
                                del acc[:sample_bytes]
                                if await speaking.muted():
                                    continue
                                try:
                                    await session.send_realtime_input(
                                        audio=types.Blob(data=chunk, mime_type="audio/pcm;rate=16000"))
                                except Exception as e:
                                    log.warning("mic send_realtime_input err: %s", e)
                                    return
                    finally:
                        pump.cancel()
                        _err_task.cancel()
                        if _fbits:
                            log.warning("mic: ffmpeg stderr: %s",
                                        b"".join(_fbits).decode(errors="replace")[:300])
            finally:
                if proc is not None:
                    try:
                        proc.kill()
                    except Exception:
                        pass

    try:
        await _run_http_capture()
    except asyncio.CancelledError:
        raise
    except Exception as e:
        log.warning("mic capture error: %s", e)
    finally:
        log.info("mic capture stopped")



# ---------------------------------------------------------------- AI audio -> WebRTC talkback
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
        if any(out_bytes):
            self.nonzero_frames += 1
            if self.recv_count <= 5 or self.nonzero_frames <= 5:
                log.info("[track] recv #%d has NONZERO audio (%d bytes nonzero)", self.recv_count, sum(1 for b in out_bytes if b))
        arr = np.frombuffer(out_bytes, dtype=np.int16).reshape(1, -1)
        fr = av.AudioFrame.from_ndarray(arr, format='s16', layout='mono')
        fr.sample_rate = self.sample_rate_out
        fr.pts = self._out_pts
        fr.time_base = Fraction(1, self.sample_rate_out)
        self._out_pts += fr.samples
        return fr


async def talkback_connect(cfg, audio_q, stream='front_doorbell_twoway'):
    """Open WebRTC consumer connection to go2rtc carrying the Gemini audio track (sendonly).
    Returns (pc, ws, mic, keepalive_task). The WebSocket MUST stay open for the session's
    lifetime (it is go2rtc's signaling + connection keepalive); closing it tears down the
    consumer. Caller closes pc/ws and cancels the task on shutdown."""
    base = cfg['FRIGATE_URL'].rstrip('/')
    wsbase = base.replace('http://','ws://').replace('https://','wss://')
    ws_url = f"{wsbase}/api/go2rtc/api/ws?src={stream}"

    rcfg = RTCConfiguration(iceServers=[])  # critical: no STUN (unreachable -> 0 candidates)
    pc = RTCPeerConnection(rcfg)
    # consumer: recvonly AUDIO only + sendonly mic (Gemini audio). NO video
    # transceiver: receiving+decoding the twoway H264 stream correlates with AD410
    # crashes (H264Decoder "Invalid data" floods during interactions). The backchannel
    # only needs audio both ways; video is watched via Frigate's normal streams, never
    # inside the talk connection (mirrors amcrest-intercom's separate video card).
    pc.addTransceiver('audio', direction='recvonly')
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
        log.info("talkback connected (AI audio -> doorbell speaker)")
        return pc, ws, mic, keep_task
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
            pc, ws, mic, keep_task = await talkback_connect(cfg, audio_q)
        except Exception as e:
            log.error("talkback connect failed: %s", e)
            return 2

        # 3. receive loop + mic capture, concurrently (echo-gated half-duplex)
        speaking = SpeakingState()
        recv_task = asyncio.create_task(gemini_receive_loop(session, audio_q, stop_ev, speaking))
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
