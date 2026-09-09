#!/usr/bin/env python3
"""Test notify_ryan WITH snapshot image attached. Sends a test notification to Ryan's phone."""
import asyncio, sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
import doorman_tools as dt

async def main():
    cfg = dt._creds()
    # Use the latest existing snapshot
    img = dt._latest_snapshot()
    print("latest snapshot:", img)
    ok, val = await dt.notify_ryan("Doorman test with snapshot image attached.", cfg, image_path=img)
    print("notify ok:", ok)
    print("value:", val)

asyncio.run(main())
