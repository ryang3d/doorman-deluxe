#!/usr/bin/env python3
"""Full run_once(25) with detailed go2rtc consumer polling (URLs) + ICE state, DEBUG logs."""
import asyncio, json, os, sys, logging, time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
import audio_bridge as ab
import aiohttp, websockets, aioice

for name, lvl in [('bridge', logging.INFO), ('aioice', logging.DEBUG),
                  ('websockets', logging.DEBUG), ('aiortc', logging.DEBUG)]:
    logging.getLogger(name).setLevel(lvl)
logging.basicConfig(level=logging.WARNING, format='%(asctime)s.%(msecs)03d %(levelname)s %(name)s %(message)s', datefmt='%H:%M:%S')
import aiohttp

async def poll_consumers(cfg, stop):
    base=cfg['FRIGATE_URL'].rstrip('/')
    async with aiohttp.ClientSession() as s:
        t=0
        while not stop.is_set():
            try:
                async with s.get(f"{base}/api/go2rtc/api/streams?src=front_doorbell_twoway") as r:
                    d=await r.json()
                cons=d.get('consumers') or []
                urls=[(c.get('url','') if isinstance(c,dict) else c) for c in cons]
                print(f"[POLL t={t}s] consumers={len(cons)} {urls}", flush=True)
            except Exception as e:
                print(f"[POLL t={t}s] ERR {e}", flush=True)
            t+=1
            try: await asyncio.wait_for(stop.wait(),timeout=1)
            except asyncio.TimeoutError: pass

async def ice_watcher(cfg, pc, stop):
    t=0
    while not stop.is_set():
        print(f"[ICE t={t}s] iceConnectionState={pc.iceConnectionState} connectionState={getattr(pc,'connectionState',None)}", flush=True)
        t+=1
        try: await asyncio.wait_for(stop.wait(),timeout=1)
        except asyncio.TimeoutError: pass

async def main():
    cfg=ab.load_config()
    stop=asyncio.Event()
    asyncio.create_task(poll_consumers(cfg, stop))
    # monkeypatch talkback_connect to expose pc to ice_watcher
    orig=ab.talkback_connect
    holder={}
    async def wrapped(cfg2,q,stream='front_doorbell_twoway'):
        pc,ws,mic,keep,recv=await asyncio.wait_for(orig(cfg2,q,stream), timeout=25)
        holder['pc']=pc
        return pc,ws,mic,keep,recv
    ab.talkback_connect=wrapped
    asyncio.create_task(ice_watcher(cfg, holder, stop))
    try:
        await asyncio.wait_for(ab.run_once(25, ab.DEFAULT_PROMPT), timeout=60)
    finally:
        stop.set()

asyncio.run(main())
