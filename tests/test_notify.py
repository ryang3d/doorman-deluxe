#!/usr/bin/env python3
"""Test notify_ryan tool: sends a test notification to Ryan's devices."""
import asyncio, sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
import doorman_tools as dt

async def main():
    cfg = dt._creds()
    ok, val = await dt.notify_ryan("Doorman test: notification path works.", cfg)
    print("notify ok:", ok)
    print("value:", val)

asyncio.run(main())
