#!/usr/bin/env python3
"""Test notify_ryan with the HA-native snapshot image. Sends a test notification to Ryan's phone."""
import asyncio, sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
import doorman_tools as dt

async def main():
    cfg = dt._creds()
    ok, val = await dt.notify_ryan("Doorman test with HA-native snapshot image attached.")
    print("notify ok:", ok)
    print("value:", val)

asyncio.run(main())
