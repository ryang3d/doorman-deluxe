#!/usr/bin/env python3
"""Isolated test of bridge's talkback_connect: connect, then query go2rtc to see if
the consumer registers and STAYS registered while we idle 15s."""
import asyncio, json, os, sys, logging
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'src'))
import audio_bridge as ab
logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')
import aiohttp

async def check_consumers(base, cookie):
    h={'Cookie':cookie} if cookie else {}
    async with aiohttp.ClientSession() as s:
        async with s.get(f"{base}/api/go2rtc/api/streams?src=front_doorbell_twoway", headers=h) as r:
            d=await r.json()
    return len(d.get('consumers') or [])

async def main():
    cfg = ab.load_config()
    base = cfg['FRIGATE_URL'].rstrip('/')
    async with aiohttp.ClientSession() as s:
        await s.post(base+'/api/login', json={'user':cfg['FRIGATE_USER'],'password':cfg['FRIGATE_PASSWORD']})
        tok=s.cookie_jar.filter_cookies(base).get('frigate_token')
        cookie='frigate_token='+tok.value if tok else ''

    q=asyncio.Queue()
    try:
        pc,ws,mic,keep = await ab.talkback_connect(cfg,q)
    except Exception as e:
        print("CONNECT FAILED:",e); return
    print("connected. checking consumers over 12s...")
    for i in range(6):
        c=await check_consumers(base,cookie)
        print(f"  t={i*2}s consumers={c}")
        await asyncio.sleep(2)
    keep.cancel()
    try: await pc.close()
    except: pass
    try: await ws.close()
    except: pass

asyncio.run(main())
