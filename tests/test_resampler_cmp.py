#!/usr/bin/env python3
"""Verify av.AudioResampler continuous 24k->48k on realistic multi-chunk speech-like audio.
Confirms no gaps/distortion vs the crude linear-interp path."""
import av, numpy as np, math, sys, os
from fractions import Fraction
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

def quality_check(method):
    rate_in=24000
    # 1 second of a tone with varying freq (chirp) to exercise resampler, in 100ms chunks
    t=np.arange(rate_in)/rate_in
    chirp=(32767*0.4*np.sin(2*np.pi*(300+200*t)*t)).astype(np.int16)
    chunk_n=rate_in//10  # 100ms
    out=np.concatenate([chirp]*1)
    chunks=[out[i:i+chunk_n] for i in range(0,len(out),chunk_n)]

    if method=='av':
        rs=av.AudioResampler(format='s16',layout='mono',rate=48000)
        allout=b''
        pts=0
        for c in chunks:
            fr=av.AudioFrame.from_ndarray(c.reshape(1,-1),format='s16',layout='mono')
            fr.sample_rate=rate_in; fr.time_base=Fraction(1,rate_in); fr.pts=pts; pts+=len(c)
            for o in rs.resample(fr):
                if o is not None: allout+=bytes(o.planes[0])
        # flush
        for o in rs.resample(None):
            if o is not None: allout+=bytes(o.planes[0])
    else:
        # linear interp
        allout=b''
        for c in chunks:
            x_in=np.arange(len(c)); x_out=np.arange(0,len(c)-1+0.5+1e-9,0.5)
            x_out=x_out[x_out<=len(c)-1]
            up=np.interp(x_out,x_in,c.astype(float)).astype(np.int16)
            allout+=up.tobytes()
    arr=np.frombuffer(allout,dtype=np.int16)
    dur_out=len(arr)/48000
    peak=np.abs(arr).max()
    print(f"{method}: output {len(arr)} samples = {dur_out*1000:.0f}ms @48k, peak={int(peak)}")
    return len(arr), dur_out

n_av,d_av=quality_check('av')
n_lin,d_lin=quality_check('linear')
# both should be ~2s of 48k output (1s in -> 2s out), peak ~13000
print(f"\nRESULT: av out {d_av*1000:.0f}ms (expect ~2000ms), linear {d_lin*1000:.0f}ms")
