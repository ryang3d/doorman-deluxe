#!/usr/bin/env python3
"""
Test consumer-mode two-way (like the browser PWA): connect ?src= and offer a
sendonly mic track + recvonly video/audio. Diagnose whether go2rtc routes our
audio to the AD410 backchannel by inspecting /api/streams while connected.

USAGE: python talkback_consumer_diag.py [--dur N] [--freq HZ] [--amp A]
amp=0 (default) = silent, safe diagnostic.
"""
import argparse, asyncio, json, logging, sys, math, array, os
import websockets, aiohttp
from fractions import Fraction
from aiortc import RTCPeerConnection, RTCSessionDescription, RTCIceCandidate, RTCConfiguration
from aiortc.mediastreams import AudioStreamTrack
from av import AudioFrame

log = logging.getLogger("diag")

def load_creds():
    d = {}
    for line in open(os.path.expanduser('~/.hermes/profiles/home-admin/frigate.env')):
        line = line.strip()
        if '=' in line and not line.startswith('#'):
            k, v = line.split('=', 1)
            d[k] = v.strip().strip('"').strip("'")
    return d

class ToneTrack(AudioStreamTrack):
    def __init__(self, freq=1000.0, duration=2.0, sample_rate=48000, amplitude=0.0):
        super().__init__()
        self.amplitude = amplitude
        self.freq = freq
        self.duration = duration
        self.sample_rate = sample_rate
        self.sample_count = int(duration * sample_rate)
        self._sent = 0; self._pts = 0; self._end = False
    async def recv(self):
        if self._end:
            raise asyncio.CancelledError
        fs = 960
        remain = self.sample_count - self._sent
        n = min(fs, remain)
        if remain <= 0:
            self._end = True
            raise asyncio.CancelledError
        samples = [int(32767*self.amplitude*math.sin(2*math.pi*self.freq*i/self.sample_rate)) for i in range(self._sent,self._sent+n)]
        while len(samples)<fs: samples.append(0)
        fr = AudioFrame(format='s16', layout='mono', samples=len(samples))
        fr.sample_rate = self.sample_rate; fr.pts=self._pts; fr.time_base=Fraction(1,self.sample_rate)
        fr.planes[0].update(array.array('h',samples).tobytes())
        self._sent+=n; self._pts+=n
        return fr

async def inspect_stream(base, cookie_header, name):
    """GET /api/streams?src=<name> and summarize how audio is routed."""
    h = {'Cookie': cookie_header} if cookie_header else {}
    async with aiohttp.ClientSession() as s:
        async with s.get(f"{base}/api/go2rtc/api/streams?src={name}", headers=h) as r:
            try:
                d = await r.json()
            except Exception:
                txt = await r.text()
                log.info("streams GET returned %s: %s", r.status, txt[:200])
                return
        # Summarize producers/consumers and whether the camera rtsp source has a backchannel sender
        print(f"\n=== go2rtc stream '{name}' topology ===")
        if isinstance(d, dict):
            for role in ('producers','consumers'):
                for item in d.get(role,[]) or []:
                    if isinstance(item, dict):
                        u = item.get('url','')
                        med = item.get('medias',[])
                        # cameras source url contains <camera-ip>
                        is_cam = '<camera-ip>' in u or 'subtype' in u
                        print(f"  {role}: {u[:90]}  medias={med}")

async def run(base, cookie, stream, dur, freq, amp):
    wsbase = base.replace('http://','ws://').replace('https://','wss://')
    # Frigate >= 0.18.0 moved the go2rtc WebRTC WS to /live/webrtc/api/ws (old
    # /api/go2rtc/api/ws now 403s via the Frigate app). No cookie needed.
    ws_url = f"{wsbase}/live/webrtc/api/ws?src={stream}"   # CONSUMER mode like the browser

    cfg = RTCConfiguration(iceServers=[])
    pc = RTCPeerConnection(cfg)
    for ev in ("iceconnectionstatechange","connectionstatechange","icegatheringstatechange"):
        pc.on(ev, lambda e=ev: log.info("%s -> %s", ev, getattr(pc, {"iceconnectionstatechange":"iceConnectionState","connectionstatechange":"connectionState","icegatheringstatechange":"iceGatheringState"}[ev])))

    # Mirror the browser: recvonly video + recvonly audio + sendonly mic track
    pc.addTransceiver('video', direction='recvonly')
    pc.addTransceiver('audio', direction='recvonly')
    mic = ToneTrack(freq=freq, duration=dur, amplitude=amp)
    pc.addTrack(mic)  # sendonly (aiortc addTrack default is sendonly)

    extra = {'Cookie': cookie} if cookie else {}
    log.info("Connecting WS %s", ws_url)
    try:
        async with websockets.connect(ws_url, additional_headers=extra, open_timeout=10) as ws:
            offer = await pc.createOffer()
            await pc.setLocalDescription(offer)
            # wait gathering complete so host candidates embedded
            for _ in range(60):
                if pc.iceGatheringState=='complete': break
                await asyncio.sleep(0.1)
            nc = sum(1 for l in pc.localDescription.sdp.splitlines() if l.lower().startswith('a=candidate'))
            log.info("gather complete, %d candidates", nc)
            await ws.send(json.dumps({'type':'webrtc/offer','value':pc.localDescription.sdp}))

            answered=asyncio.Event(); werr=None
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
                            if cs: await add_cand(pc, cs)
                        elif t=='error':
                            werr=m.get('value'); log.error("go2rtc: %s", werr); answered.set()
                except asyncio.TimeoutError:
                    log.warning("recv timeout"); answered.set()
                except Exception as e:
                    log.warning("recv ended: %s", e); answered.set()
            task=asyncio.create_task(consume())
            try:
                await asyncio.wait_for(answered.wait(),timeout=30)
            except asyncio.TimeoutError:
                log.warning("no answer within 30s")
            if werr: return 2

            # wait ICE
            for _ in range(40):
                if pc.iceConnectionState in ('connected','completed'): break
                if pc.iceConnectionState in ('failed','disconnected','closed'):
                    log.warning("ICE %s", pc.iceConnectionState); break
                await asyncio.sleep(0.5)
            log.info("Final ICE: %s", pc.iceConnectionState)
            task.cancel()

            if pc.iceConnectionState in ('connected','completed'):
                # Inspect go2rtc topology while connected (does camera source get our audio?)
                await inspect_stream(base, cookie, stream)
                await asyncio.sleep(2)
                await inspect_stream(base, cookie, stream)
                return 0
            return 3
    except Exception as e:
        log.error("failed: %s: %s", type(e).__name__, e)
        return 4

async def add_cand(pc, cand_str):
    try:
        cand_str=cand_str.strip()
        if cand_str.startswith('a='): cand_str=cand_str[2:]
        parts=cand_str.split(' ')
        if len(parts)>=6 and parts[0].startswith('candidate:'):
            c=RTCIceCandidate(foundation=parts[0].split(':')[1],component=int(parts[1]),protocol=parts[2],priority=int(parts[3]),ip=parts[4],port=int(parts[5]),type=parts[7] if len(parts)>7 else 'host')
            c.sdpMid='0'; c.sdpMLineIndex=0
            await pc.addIceCandidate(c)
    except Exception as e:
        log.debug("cand err %s", e)

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--dur',type=float,default=3.0)
    ap.add_argument('--freq',type=float,default=1000.0)
    ap.add_argument('--amp',type=float,default=0.0)
    ap.add_argument('--stream',default='front_doorbell_twoway')
    ap.add_argument('-v','--verbose',action='store_true')
    a=ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,format='%(levelname)s %(message)s')
    c=load_creds(); base=c['FRIGATE_URL'].rstrip('/')
    async def gc():
        async with aiohttp.ClientSession() as s:
            async with s.post(base+'/api/login',json={'user':c['FRIGATE_USER'],'password':c['FRIGATE_PASSWORD']}) as r:
                tok=s.cookie_jar.filter_cookies(base).get('frigate_token')
                return 'frigate_token='+tok.value if tok else ''
    cookie=asyncio.run(gc())
    log.info("cookie: %s", bool(cookie))
    code=asyncio.run(run(base,cookie,a.stream,a.dur,a.freq,a.amp))
    sys.exit(code)

if __name__=='__main__':
    main()
