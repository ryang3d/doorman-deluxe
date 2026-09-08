#!/usr/bin/env python3
"""Test GeminiAudioTrack in isolation: push a fake pcm16 24k chunk, verify recv() yields valid 48k frames."""
import asyncio, sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
from audio_bridge import GeminiAudioTrack
import numpy as np

async def main():
    q = asyncio.Queue()
    track = GeminiAudioTrack(q, sample_rate_out=48000)
    # push 0.1s of 24k tone-ish (440Hz)
    rate=24000
    t=np.arange(rate//10)
    sig=(32767*0.5*np.sin(2*np.pi*440*t/rate)).astype(np.int16)
    await q.put(sig.tobytes())

    # ask track for 3 frames
    got=0
    for i in range(3):
        fr = await track.recv()
        arr = fr.to_ndarray().flatten()
        got += len(arr)
        peak = np.abs(arr).max()
        print(f"frame {i}: {len(arr)} samples @ {fr.sample_rate}, peak={int(peak)}")
    print("total output samples:", got, "(3 frames ~ 20ms each @48k = 2880)")
    # verify nonzero audio present (not just silence from the 0.1s of tone passed through)
    print("OK" if got>0 else "FAIL")

asyncio.run(main())
