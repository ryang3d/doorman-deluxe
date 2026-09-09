#!/usr/bin/env python3
"""Unit-test ActivityClock idle tracking in doorman.py."""
import asyncio, sys, os, time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
from doorman import ActivityClock

async def main():
    c = ActivityClock()
    # fresh: idle ~0
    assert await c.idle_seconds() < 1.0, "should start not-idle"
    # wait 0.3s, idle grows
    await asyncio.sleep(0.3)
    idle = await c.idle_seconds()
    assert 0.2 < idle < 0.6, f"idle should be ~0.3s, got {idle}"
    # mark resets idle
    await c.mark()
    assert await c.idle_seconds() < 0.1, "mark should reset idle to ~0"
    # wait again, idle grows past a threshold
    await asyncio.sleep(0.4)
    idle2 = await c.idle_seconds()
    assert idle2 > 0.3, f"idle should have grown, got {idle2}"
    print(f"PASS: ActivityClock idle tracking correct (idle grew to {idle2:.2f}s, reset on mark)")
    return 0

sys.exit(asyncio.run(main()))
