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
  FRIGATE_URL, FRIGATE_USER, FRIGATE_PASSWORD, GEMINI_API_KEY
Camera mic RTSP creds are in the doorbell go2rtc stream / hardcoded below (admin/<doorbell-pass>)
  - better: read from config, but keep in one place.

Run:
  python src/audio_bridge.py            # run one interaction until Ctrl-C / timeout
  python src/audio_bridge.py --once 30  # run a fixed-duration session then exit
"""
import argparse, asyncio, json, logging, math, array, subprocess, sys, os
from fractions import Fraction
import websockets, aiohttp
import av

# aiortc
from aiortc import RTCPeerConnection, RTCSessionDescription, RTCIceCandidate, RTCConfiguration
from aiortc.mediastreams import AudioStreamTrack

log = logging.getLogger("bridge")

# ---------------------------------------------------------------- config
CONFIG_ENV = '~/.hermes/profiles/home-admin/frigate.env'

def load_config():
    d = {}
    for line in open(CONFIG_ENV):
        line = line.strip()
        if '=' in line and not line.startswith('#'):
            k, v = line.split('=', 1)
            d[k] = v.strip().strip('"').strip("'")
    return d

# Camera mic RTSP (verified capture path, Task 0.2). Creds match go2rtc stream.
CAM_MIC_RTSP = "rtsp://admin:<doorbell-pass>@<camera-ip>:554/cam/realmonitor?channel=1&subtype=1"

# ---------------------------------------------------------------- Gemini Live session
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
    if system_prompt:
        connect_cfg["system_instruction"] = {"parts": [{"text": system_prompt}]}
    cm = client.aio.live.connect(model=model, config=connect_cfg)
    log.info("Gemini Live connect CM ready (%s)", model)
    return cm


async def gemini_receive_loop(session, audio_out_q, stop_ev):
    """Pull Gemini responses; push outbound pcm16 (24k) chunks to audio_out_q."""
    try:
        async for response in session.receive():
            sc = getattr(response, 'server_content', None)
            if not sc:
                continue
            ot = getattr(sc, 'output_transcription', None)
            if ot and ot.text:
                log.info("[gemini said] %s", ot.text)
            mt = getattr(sc, 'model_turn', None)
            if mt:
                for part in (mt.parts or []):
                    if getattr(part, 'inline_data', None) and part.inline_data.data:
                        data = part.inline_data.data
                        # native audio pcm16 at 24kHz mono (verified Task 1.3)
                        audio_out_q.put_nowait(bytes(data))
            if getattr(sc, 'turn_complete', False):
                log.info("turn complete")
    except asyncio.CancelledError:
        pass
    except Exception as e:
        log.warning("gemini receive ended: %s", e)
    finally:
        stop_ev.set()


# ---------------------------------------------------------------- mic capture -> Gemini
async def mic_to_gemini(session, stop_ev, sample_bytes=3200):
    """ffmpeg reads AD410 mic (RTSP) as raw pcm16 16k mono; forward chunks to Gemini.
    sample_bytes = 0.1s of 16k mono 16-bit = 3200 bytes."""
    import threading
    cmd = [
        'ffmpeg', '-hide_banner', '-loglevel', 'error',
        '-rtsp_transport', 'tcp',
        '-i', CAM_MIC_RTSP,
        '-f', 's16le', '-acodec', 'pcm_s16le', '-ar', '16000', '-ac', '1',
        'pipe:1',
    ]
    log.info("starting mic capture: %s", CAM_MIC_RTSP.split('@')[1])
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    # Drain stderr so ffmpeg doesn't block
    def drain_err():
        for line in proc.stderr:
            pass
    threading.Thread(target=drain_err, daemon=True).start()

    try:
        while not stop_ev.is_set():
            data = proc.stdout.read(sample_bytes)
            if not data:
                log.warning("mic capture ended (ffmpeg closed)")
                break
            if len(data) == sample_bytes:
                from google.genai import types
                try:
                    await session.send_realtime_input(
                        audio=types.Blob(data=data, mime_type="audio/pcm;rate=16000"))
                except Exception as e:
                    log.warning("send_realtime_input err: %s", e)
                    break
            await asyncio.sleep(0)
    finally:
        try:
            proc.terminate()
        except Exception:
            pass
        log.info("mic capture stopped")


# ---------------------------------------------------------------- AI audio -> WebRTC talkback
class GeminiAudioTrack(AudioStreamTrack):
    """Sendonly track. Pulls pcm16 24k chunks from queue, resamples to 48k for opus.
    Emits 20ms frames of 48k mono s16."""
    def __init__(self, audio_q, sample_rate_out=48000):
        super().__init__()
        self.audio_q = audio_q
        self.sample_rate_out = sample_rate_out
        self._resampler = av.AudioResampler(
            format='s16', layout='mono', rate=sample_rate_out)
        self._out_pts = 0          # PTS in OUTPUT sample clock
        self._buf = b''            # resampled 48k pcm16 output bytes awaiting emission
        self._ended = False

    async def recv(self):
        if self._ended:
            raise asyncio.CancelledError
        import numpy as np
        target_bytes = int(self.sample_rate_out * 0.02) * 2  # 20ms @48k mono = 1920 bytes
        # gather input until we have >= one output frame buffered
        guard = 0
        while len(self._buf) < target_bytes and guard < 200:
            try:
                chunk = await asyncio.wait_for(self.audio_q.get(), timeout=0.4)
            except asyncio.TimeoutError:
                chunk = b'\x00' * 4800   # 0.1s 24k silence keeps track alive
            except asyncio.CancelledError:
                raise
            # chunk is pcm16 24k mono -> resample to 48k
            arr = np.frombuffer(chunk if len(chunk) % 2 == 0 else chunk + b'\x00',
                                dtype=np.int16).reshape(1, -1)
            frame = av.AudioFrame.from_ndarray(arr, format='s16', layout='mono')
            frame.sample_rate = 24000
            frame.time_base = Fraction(1, 24000)
            frame.pts = None  # let av assign; we track output pts separately
            try:
                for fr in self._resampler.resample(frame):
                    if fr is not None:
                        self._buf += bytes(fr.planes[0])
            except Exception as e:
                log.debug("resample err: %s", e)
            guard += 1
        # emit exactly one 20ms output frame
        out_bytes = self._buf[:target_bytes]
        self._buf = self._buf[target_bytes:]
        if len(out_bytes) < target_bytes:
            out_bytes += b'\x00' * (target_bytes - len(out_bytes))
        arr = np.frombuffer(out_bytes, dtype=np.int16).reshape(1, -1)
        fr = av.AudioFrame.from_ndarray(arr, format='s16', layout='mono')
        fr.sample_rate = self.sample_rate_out
        fr.pts = self._out_pts
        fr.time_base = Fraction(1, self.sample_rate_out)
        self._out_pts += fr.samples
        return fr


async def talkback_connect(cfg, audio_q, stream='front_doorbell_twoway'):
    """Open WebRTC consumer connection to go2rtc carrying the Gemini audio track (sendonly)."""
    from google.genai import types as _  # noqa
    base = cfg['FRIGATE_URL'].rstrip('/')
    wsbase = base.replace('http://','ws://').replace('https://','wss://')
    ws_url = f"{wsbase}/api/go2rtc/api/ws?src={stream}"

    rcfg = RTCConfiguration(iceServers=[])  # critical: no STUN (unreachable -> 0 candidates)
    pc = RTCPeerConnection(rcfg)
    # consumer: recvonly video + audio, plus sendonly mic (Gemini audio)
    pc.addTransceiver('video', direction='recvonly')
    pc.addTransceiver('audio', direction='recvonly')
    mic = GeminiAudioTrack(audio_q)
    pc.addTrack(mic)

    log.info("connecting WS %s", ws_url)
    async with websockets.connect(ws_url, open_timeout=10) as ws:
        offer = await pc.createOffer()
        await pc.setLocalDescription(offer)
        for _ in range(80):
            if pc.iceGatheringState == 'complete':
                break
            await asyncio.sleep(0.1)
        await ws.send(json.dumps({'type':'webrtc/offer','value':pc.localDescription.sdp}))
        log.info("offer sent")
        # consume signaling concurrently
        answered = asyncio.Event(); werr=None
        async def consume():
            nonlocal werr
            try:
                while True:
                    raw=await asyncio.wait_for(ws.recv(),timeout=20)
                    m=json.loads(raw); t=m.get('type')
                    if t=='webrtc/answer':
                        await pc.setRemoteDescription(RTCSessionDescription(type='answer',sdp=m['value']))
                        log.info("got answer"); answered.set()
                    elif t=='webrtc/candidate':
                        cs=m.get('value')
                        if cs: await add_candidate(pc, cs)
                    elif t=='error':
                        werr=m.get('value'); log.error("go2rtc: %s", werr); answered.set()
            except (asyncio.TimeoutError, Exception):
                answered.set()
        ctask=asyncio.create_task(consume())
        try:
            await asyncio.wait_for(answered.wait(),timeout=30)
        except asyncio.TimeoutError:
            log.warning("no answer")
        if werr: raise RuntimeError(f"go2rtc: {werr}")
        for _ in range(40):
            if pc.iceConnectionState in ('connected','completed'): break
            if pc.iceConnectionState in ('failed','disconnected','closed'):
                log.warning("ICE %s", pc.iceConnectionState); break
            await asyncio.sleep(0.5)
        log.info("ICE state: %s", pc.iceConnectionState)
        if pc.iceConnectionState not in ('connected','completed'):
            ctask.cancel()
            await pc.close()
            raise RuntimeError("ICE never connected")
        log.info("talkback connected (AI audio -> doorbell speaker)")
        ctask.cancel()
        # Keep pc alive; yield the connection. We return pc and ws but caller holds session open.
        return pc, ws, mic


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
            pc, ws, mic = await talkback_connect(cfg, audio_q)
        except Exception as e:
            log.error("talkback connect failed: %s", e)
            return 2

        # 3. receive loop + mic capture, concurrently
        recv_task = asyncio.create_task(gemini_receive_loop(session, audio_q, stop_ev))
        mic_task = asyncio.create_task(mic_to_gemini(session, stop_ev))
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
        recv_task.cancel(); mic_task.cancel()
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
