#!/usr/bin/env python3
"""EXACT mic_to_gemini path: connect talkback, then on the event loop do the FIRST
proc.stdout.read(3200) with NO warmup (ffmpeg RTSP takes seconds to produce first chunk).
Measures the event-loop freeze duration and whether the go2rtc consumer drops."""
import asyncio, json, os, sys, logging, subprocess, threading, time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
import audio_bridge as ab
logging.basicConfig(level=logging.ERROR)
import aiohttp
CAM = ab.CAM_MIC_RTSP
def log(*a): print(f"[{time.strftime('%H:%M:%S')}]", *a, flush=True)

async def consumers(cfg, s, cookie):
    async with s.get(f"{cfg['FRIGATE_URL'].rstrip('/')}/api/go2rtc/api/streams?src=front_doorbell_twoway", headers={'Cookie':cookie}) as r:
        return len((await r.json()).get('consumers') or [])

async def main():
    cfg=ab.load_config(); base=cfg['FRIGATE_URL'].rstrip('/')
    async with aiohttp.ClientSession() as s:
        await s.post(base+'/api/login',json={'user':cfg['FRIGATE_USER'],'password':cfg['FRIGATE_PASSWORD']})
        tok=s.cookie_jar.filter_cookies(base).get('frigate_token'); cookie='frigate_token='+tok.value if tok else ''
        q=asyncio.Queue()
        pc,ws,mic,keep,_recv=await asyncio.wait_for(ab.talkback_connect(cfg,q), timeout=25)
        await asyncio.sleep(1)
        log("connected, consumers:", await consumers(cfg,s,cookie))

        cmd=['ffmpeg','-hide_banner','-loglevel','error','-rtsp_transport','tcp',
             '-i',CAM,'-f','s16le','-acodec','pcm_s16le','-ar','16000','-ac','1','pipe:1']
        proc=subprocess.Popen(cmd,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        threading.Thread(target=lambda:[l for l in proc.stderr],daemon=True).start()
        log("ffmpeg started; about to do the FIRST blocking stdout.read(3200) on the event loop (no warmup)")

        # a timer task that would keep the WS/ICE alive in a healthy loop
        async def heartbeat():
            n=0
            while True:
                await asyncio.sleep(1); n+=1
                log(f"  [heartbeat alive at +{n}s] consumers={await consumers(cfg,s,cookie)}")
        hb=asyncio.create_task(heartbeat())

        # measure event-loop freeze from a helper scheduled BEFORE the blocking read
        loop=asyncio.get_event_loop()
        start=time.monotonic()
        frozen_until=[None]
        loop.call_soon(lambda: frozen_until.__setitem__(0, time.monotonic()))
        t0=time.monotonic()
        try:
            data=proc.stdout.read(3200)   # BLOCKING, exact mic_to_gemini call
        finally:
            freeze=time.monotonic()-t0
            log(f"  FIRST blocking read returned after {freeze:.2f}s (event loop frozen this long); got {len(data) if data else 0} bytes")
        # now loop stays running; watch consumers
        async def read_loop():
            try:
                while True:
                    d=proc.stdout.read(3200)
                    if not d: break
            except asyncio.CancelledError: pass
        rl=asyncio.create_task(read_loop())
        for i in range(4):
            await asyncio.sleep(2)
            log(f"  post-first-read t={int((i+1)*2)}s consumers={await consumers(cfg,s,cookie)}")
        rl.cancel(); hb.cancel(); proc.terminate()
        keep.cancel()
        try: await pc.close()
        except: pass
        try: await ws.close()
        except: pass

asyncio.run(main())
