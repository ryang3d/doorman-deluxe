#!/usr/bin/env python3
"""Test: does starting the AD410 mic RTSP capture (via ffmpeg) drop the talkback consumer?
Connects talkback (consumers=2 expected), then starts mic ffmpeg, checks consumers."""
import asyncio, json, os, sys, logging, subprocess
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'src'))
import audio_bridge as ab
logging.basicConfig(level=logging.WARNING)
import aiohttp

async def consumers(cfg, s, cookie):
    base=cfg['FRIGATE_URL'].rstrip('/')
    h={'Cookie':cookie}
    async with s.get(f"{base}/api/go2rtc/api/streams?src=front_doorbell_twoway", headers=h) as r:
        d=await r.json()
    return len(d.get('consumers') or [])

async def main():
    cfg=ab.load_config(); base=cfg['FRIGATE_URL'].rstrip('/')
    async with aiohttp.ClientSession() as s:
        await s.post(base+'/api/login',json={'user':cfg['FRIGATE_USER'],'password':cfg['FRIGATE_PASSWORD']})
        tok=s.cookie_jar.filter_cookies(base).get('frigate_token'); cookie='frigate_token='+tok.value if tok else ''

        q=asyncio.Queue()
        pc,ws,mic,keep = await ab.talkback_connect(cfg,q)
        print("talkback connected, checking consumer...")
        await asyncio.sleep(1)
        print("  consumers before mic:", await consumers(cfg,s,cookie))

        # Start the ffmpeg mic capture (same command the bridge uses)
        import threading
        cmd=['ffmpeg','-hide_banner','-loglevel','error','-rtsp_transport','tcp',
             '-i',ab.CAM_MIC_RTSP,'-f','s16le','-acodec','pcm_s16le','-ar','16000','-ac','1','pipe:1']
        proc=subprocess.Popen(cmd,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        def drain():
            for line in proc.stderr: pass
        threading.Thread(target=drain,daemon=True).start()
        print("mic ffmpeg started, watching consumers...")
        for i in range(6):
            await asyncio.sleep(2)
            c=await consumers(cfg,s,cookie)
            print(f"  t={i*2+1}s consumers={c}")
            if c==0:
                print("  >>> CONSUMER DROPPED after mic started <<<")
                break
        proc.terminate()
        keep.cancel()
        try: await pc.close()
        except: pass
        try: await ws.close()
        except: pass

asyncio.run(main())
