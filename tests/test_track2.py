#!/usr/bin/env python3
"""Test the non-blocking GeminiAudioTrack: push realistic 24k audio bursts, verify recv()
returns steady 20ms@48k frames without blocking/dropping, and audio is continuous + audible."""
import asyncio, sys, os, time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
from audio_bridge import GeminiAudioTrack
import numpy as np

async def main():
    q = asyncio.Queue()
    track = GeminiAudioTrack(q, sample_rate_out=48000)

    # simulate Gemini: push 0.5s of 24k speech-like tone in several burst chunks
    rate = 24000
    sig = (32767*0.4*np.sin(2*np.pi*220*np.arange(rate//2)/rate)).astype(np.int16)
    # push as 5 chunks of ~100ms
    chunkbytes = (rate//10)*2
    for i in range(0, len(sig.tobytes()), chunkbytes):
        await q.put(sig.tobytes()[i:i+chunkbytes])
    await q.put(b'')  # sentinel? not used; fine

    # Pull ~40 frames fast (should be non-blocking and cover 0.5s audio => 25 frames @20ms, plus zeros)
    frames=[]
    start=time.time()
    for i in range(40):
        fr = await track.recv()   # must NOT block >~50ms if idle
        frames.append(fr)
    elapsed=time.time()-start
    print(f"40 recv() calls took {elapsed*1000:.0f} ms ({elapsed/40*1000:.1f} ms avg/frame)")

    # check pacing continuity: PTS should step by 960 each frame
    pts = [f.pts for f in frames]
    diffs = [pts[i+1]-pts[i] for i in range(len(pts)-1)]
    print("PTS diffs all 960:", all(d==960 for d in diffs), "| sample_rate:", frames[0].sample_rate)

    # check audio present (non-silence) in the frames that cover the tone window
    allaud = np.concatenate([f.to_ndarray().flatten() for f in frames])
    peak = np.abs(allaud).max()
    nonzero = np.count_nonzero(allaud)
    print(f"total frames audio peak={int(peak)}, nonzero samples={nonzero}/{len(allaud)}")
    print("RESULT:", "PASS" if (all(d==960 for d in diffs) and peak>0 and elapsed/40 < 0.05) else "CHECK")

asyncio.run(main())
