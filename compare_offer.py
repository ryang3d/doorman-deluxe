#!/usr/bin/env python3
"""Compare the SDP offer the working tone script produces vs the bridge's talkback_connect,
to find why go2rtc registers one as a consumer and not the other."""
import asyncio, os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'src'))
from aiortc import RTCPeerConnection, RTCConfiguration
from aiortc.mediastreams import AudioStreamTrack
import numpy as np

class DummyMic(AudioStreamTrack):
    async def recv(self):
        raise asyncio.CancelledError

async def make_offer(mode):
    cfg = RTCConfiguration(iceServers=[])
    pc = RTCPeerConnection(cfg)
    if mode == 'tone':   # exactly like talkback_consumer_diag.py
        pc.addTransceiver('video', direction='recvonly')
        pc.addTransceiver('audio', direction='recvonly')
        pc.addTrack(DummyMic())
    elif mode == 'bridge':
        # import the actual bridge track + replicate talkback_connect's transceivers
        from audio_bridge import GeminiAudioTrack
        q = asyncio.Queue()
        pc.addTransceiver('video', direction='recvonly')
        pc.addTransceiver('audio', direction='recvonly')
        mic = GeminiAudioTrack(q)
        pc.addTrack(mic)
    offer = await pc.createOffer()
    await pc.setLocalDescription(offer)
    for _ in range(80):
        if pc.iceGatheringState == 'complete':
            break
        await asyncio.sleep(0.1)
    await pc.close()
    return pc.localDescription.sdp

async def main():
    s_tone = await make_offer('tone')
    s_bridge = await make_offer('bridge')
    print("===== TONE SDP audio+video m-lines =====")
    for l in s_tone.splitlines():
        if l.startswith(('m=','a=mid','a=sendrecv','a=sendonly','a=recvonly','a=rtpmap','a=setup')):
            print(l)
    print()
    print("===== BRIDGE SDP audio+video m-lines =====")
    for l in s_bridge.splitlines():
        if l.startswith(('m=','a=mid','a=sendrecv','a=sendonly','a=recvonly','a=rtpmap','a=setup')):
            print(l)

asyncio.run(main())
