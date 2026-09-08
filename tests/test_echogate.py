#!/usr/bin/env python3
"""Unit-test the SpeakingState echo gate logic."""
import asyncio, sys, os, time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
from audio_bridge import SpeakingState

async def main():
    s = SpeakingState(tail_s=1.0)
    # initially not muted
    assert await s.muted() is False, "should start unmuted"
    # mark active -> muted
    await s.mark_active()
    assert await s.muted() is True, "muted while active"
    # mark idle -> still muted during tail
    await s.mark_idle()
    assert await s.muted() is True, "muted during tail after idle"
    # wait past tail -> unmuted
    await asyncio.sleep(1.2)
    assert await s.muted() is False, "unmuted after tail"
    print("SpeakingState echo-gate logic PASS")

asyncio.run(main())
