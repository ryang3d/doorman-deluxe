#!/usr/bin/env python3
"""Direct test of the notify_ryan tool end-to-end (pushes to Ryan's phone)."""
import asyncio, sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
import doorman_tools as dt

async def main():
    cfg = dt._creds()
    ok, val = await dt.notify_ryan("Doorman notify tool test: wiring confirmed working.", cfg)
    print("notify ok:", ok)
    print("value:", val)

asyncio.run(main())
