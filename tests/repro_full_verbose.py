#!/usr/bin/env python3
"""Run run_once(25) with bridge INFO logs AND poll go2rtc consumers, to correlate."""
import asyncio, json, os, sys, logging
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
import audio_bridge as ab
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s %(message)s', datefmt='%H:%M:%S')
import aiohttp

async def poll_consumers(cfg, stop):
    base=cfg['FRIGATE_URL'].rstrip('/')
    async with aiohttp.ClientSession() as s:
        r = await s.post(base+'/api/login',json={'user':cfg['FRIGATE_USER'],'password':cfg['FRIGATE_PASSWORD']})
        tok=s.cookie_jar.filter_cookies(base).get('frigate_token'); cookie='frigate_token='+tok.value if tok else ''
        print(f"[POLL auth status={r.status} cookie={'yes' if cookie else 'NO'}]", flush=True)
        h={'Cookie':cookie}
        t=0
        while not stop.is_set():
            async with s.get(f"{base}/api/go2rtc/api/streams?src=front_doorbell_twoway", headers=h) as resp:
                txt=await resp.text()
                d=json.loads(txt) if txt else {}
            c=len(d.get('consumers') or [])
            print(f"[POLL t={t}s] consumers={c}", flush=True)
            t+=2
            try: await asyncio.wait_for(stop.wait(),timeout=2)
            except asyncio.TimeoutError: pass

async def main():
    cfg=ab.load_config()
    stop=asyncio.Event()
    p=asyncio.create_task(poll_consumers(cfg, stop))
    try:
        await asyncio.wait_for(ab.run_once(25, ab.DEFAULT_PROMPT), timeout=60)
    finally:
        stop.set()
        p.cancel()

asyncio.run(main())
