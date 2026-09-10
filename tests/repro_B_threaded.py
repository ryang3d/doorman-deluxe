#!/usr/bin/env python3
"""PHASE B repro: SAME mic ffmpeg read loop but offloaded to a thread via
asyncio.to_thread -> event loop stays free -> consumer must stay registered."""
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
        log("ffmpeg started, warming up...")
        await asyncio.to_thread(proc.stdout.read, 3200)
        log("now running THREADED read loop (asyncio.to_thread); polling consumers...")

        async def read_loop():
            try:
                while True:
                    data=await asyncio.to_thread(proc.stdout.read, 3200)
                    if not data: break
            except asyncio.CancelledError: pass
        task=asyncio.create_task(read_loop())
        for i in range(5):
            await asyncio.sleep(2)
            log(f"  t={int((i+1)*2)}s consumers={await consumers(cfg,s,cookie)}")
        task.cancel(); proc.terminate()
        keep.cancel()
        try: await pc.close()
        except: pass
        try: await ws.close()
        except: pass

asyncio.run(main())
