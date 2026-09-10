#!/usr/bin/env python3
"""PHASE A repro: blocking proc.stdout.read() on the event loop drops the go2rtc consumer.
Warms up ffmpeg first (so reads return ~100ms, not forever), then blocks the loop with
synchronous reads exactly like mic_to_gemini."""
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

        # start ffmpeg + warm it up so stdout produces promptly
        cmd=['ffmpeg','-hide_banner','-loglevel','error','-rtsp_transport','tcp',
             '-i',CAM,'-f','s16le','-acodec','pcm_s16le','-ar','16000','-ac','1','pipe:1']
        proc=subprocess.Popen(cmd,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        threading.Thread(target=lambda:[l for l in proc.stderr],daemon=True).start()
        log("ffmpeg started, warming up (read 1 chunk in a thread)...")
        await asyncio.to_thread(proc.stdout.read, 3200)
        log("ffmpeg producing data; now starting BLOCKING read loop on event loop")

        started=time.monotonic()
        try:
            while time.monotonic()-started < 10:
                data=proc.stdout.read(3200)   # BLOCKING sync read on the loop
                if not data: break
                await asyncio.sleep(0)
                if (int((time.monotonic()-started)/2) != int((time.monotonic()-started-0.5)/2)):
                    pass
        finally:
            proc.terminate()
        # poll consumer a few times from main loop
        for i in range(3):
            log(f"post-read t={i}s consumers={await consumers(cfg,s,cookie)}")
            await asyncio.sleep(1)
        keep.cancel()
        try: await pc.close()
        except: pass
        try: await ws.close()
        except: pass

asyncio.run(main())
