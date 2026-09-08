#!/usr/bin/env python3
"""Reproduce the consumer drop: connect via talkback_connect AND feed real audio into
the queue (as Gemini would) while running the track recv, to see if the consumer drops."""
import asyncio, json, os, sys, logging, numpy as np
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'src'))
import audio_bridge as ab
logging.basicConfig(level=logging.WARNING)
import aiohttp

async def check_consumers(base, cookie):
    h={'Cookie':cookie} if cookie else {}
    async with aiohttp.ClientSession() as s:
        async with s.get(f"{base}/api/go2rtc/api/streams?src=front_doorbell_twoway", headers=h) as r:
            d=await r.json()
    return len(d.get('consumers') or [])

async def feed_audio(q, seconds=8):
    """Push synthetic 'Gemini-like' 24k audio bursts for `seconds`."""
    rate=24000
    sig=(32767*0.3*np.sin(2*np.pi*300*np.arange(rate)//rate)).astype(np.int16)
    chunk=4000  # ~83ms
    nchunks=int(seconds*rate/chunk)
    for i in range(nchunks):
        await q.put(sig[:chunk].tobytes())
        await asyncio.sleep(0.02)
    await q.put(None)  # end

async def main():
    cfg=ab.load_config(); base=cfg['FRIGATE_URL'].rstrip('/')
    async with aiohttp.ClientSession() as s:
        await s.post(base+'/api/login',json={'user':cfg['FRIGATE_USER'],'password':cfg['FRIGATE_PASSWORD']})
        tok=s.cookie_jar.filter_cookies(base).get('frigate_token'); cookie='frigate_token='+tok.value if tok else ''

    q=asyncio.Queue()
    pc,ws,mic,keep = await ab.talkback_connect(cfg,q)
    print("connected")
    # start the track's recv loop (drains queue -> sends to go2rtc)
    async def drive_track():
        try:
            while True:
                await mic.recv()
                await asyncio.sleep(0)
        except asyncio.CancelledError:
            pass
    drive=asyncio.create_task(drive_track())
    feed=asyncio.create_task(feed_audio(q,8))
    # check consumers over time
    for i in range(8):
        c=await check_consumers(base,cookie)
        print(f"t={i*1.5:.0f}s consumers={c}")
        await asyncio.sleep(1.5)
    feed.cancel(); drive.cancel(); keep.cancel()
    try: await pc.close()
    except: pass
    try: await ws.close()
    except: pass

asyncio.run(main())
