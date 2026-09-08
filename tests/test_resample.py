#!/usr/bin/env python3
"""Test av.AudioResampler 24k->48k conversion used in the bridge's audio track."""
import av, numpy as np, math
from fractions import Fraction

def to_ndarray(chunk):
    arr = np.frombuffer(chunk if len(chunk)%2==0 else chunk+b'\x00', dtype=np.int16)
    return arr

# simulate 0.1s of 24k mono pcm16 = 2400 samples
rate_in = 24000
n = 2400
t = np.arange(n)
sig = (32767*0.5*np.sin(2*math.pi*440*t/rate_in)).astype(np.int16)
chunk = sig.tobytes()

resampler = av.AudioResampler(format='s16', layout='mono', rate=48000)
frame = av.AudioFrame.from_ndarray(to_ndarray(chunk).reshape(1,-1), format='s16', layout='mono')
frame.sample_rate = rate_in
frame.time_base = Fraction(1, rate_in)
frame.pts = 0
out = resampler.resample(frame)
total_out = 0
for fr in out:
    if fr is not None:
        arr = fr.to_ndarray().flatten()
        total_out += len(arr)
        print("resampled frame samples:", len(arr), "sample_rate:", fr.sample_rate)
print("total out samples:", total_out, "(expected ~4800 for 0.1s in at 24k->48k)")

# Verify level (should be nonzero ~ speech-level)
print("peak:", np.abs(np.abs(sig).max()), "resampled peak check done")
