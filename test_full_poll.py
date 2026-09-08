#!/usr/bin/env python3
"""Run the full run_once but poll go2rtc consumers every 2s to see when they drop."""
import asyncio, json, os, sys, logging, threading
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'src'))
import audio_bridge as ab
logging.basicConfig(level=logging.WARNING, format='%(levelname)s %(message)s')
import aiohttp

async def poll_consumers(cfg, stop):
    base=cfg['FRIGATE_URL'].rstrip('/')
    async with aiohttp.ClientSession() as s:
        await s.post(base+'/api/login',json={'user':cfg['FRIGATE_USER'],'password':cfg['FRIGATE_PASSWORD']})
        tok=s.cookie_jar.filter_cookies(base).get('frigate_token'); cookie='frigate_token='+tok.value if tok else ''
        h={'Cookie':cookie}
        t=0
        while not stop.is_set():
            async with s.get(f"{base}/api/go2rtc/api/streams?src=front_doorbell_twoway", headers=h) as r:
                d=await r.json()
            c=len(d.get('consumers') or [])
            print(f"[poll t={t}s] consumers={c}", flush=True)
            t+=2
            try: await asyncio.wait_for(stop.wait(),timeout=2)
            except asyncio.TimeoutError: pass

async def main():
    cfg=ab.load_config()
    stop=asyncio.Event()
    # poll every 2s while run_once runs
    from audio_bridge import run_once
    p=asyncio.create_task(poll_consumers(cfg, stop))
    # run 20s
    await asyncio.wait_for(run_once(20, ab.DEFAULT_PROMPT), timeout=40)
    stop.set()

asyncio.run(main())
