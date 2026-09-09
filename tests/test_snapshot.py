#!/usr/bin/env python3
"""Test snapshot_front_door tool against the real Frigate (read-only + saves a jpg).
Does NOT send a notification."""
import asyncio, sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
import doorman_tools as dt

async def main():
    cfg = dt._creds()
    print("creds: frigate_url=%s hass_url=%s" % (cfg['frigate_url'], cfg['hass_url']))
    ok, val = await dt.snapshot_front_door(cfg)
    print("snapshot ok:", ok)
    print("value:", val)
    if ok:
        p = val
        print("file exists:", os.path.exists(p), "size:", os.path.getsize(p) if os.path.exists(p) else 0)

asyncio.run(main())
