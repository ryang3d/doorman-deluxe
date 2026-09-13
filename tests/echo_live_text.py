#!/usr/bin/env python3
"""Phase 1 Task 1.3: deterministic Gemini Live audio test.
Sends a TEXT prompt asking Gemini to speak, captures the returned audio (round-trip
of the AUDIO output path) plus any transcription. This validates the model string,
Live connection, and native-audio output without needing speech in an input wav.
"""
import asyncio, os, sys, wave

def load_key():
    for line in open(os.path.expanduser('~/.hermes/profiles/home-admin/frigate.env')):
        if line.startswith('GEMINI_API_KEY='):
            return line.split('=',1)[1].strip().strip('"').strip("'")
    return None

async def main():
    key = load_key()
    if not key:
        print("NO GEMINI_API_KEY"); return 1
    from google import genai
    from google.genai import types

    model = "gemini-3.1-flash-live-preview"
    config = {"response_modalities":["AUDIO"]}
    client = genai.Client(api_key=key)
    audio_out = bytearray()
    transcripts=[]

    print(f"connecting {model} ...")
    try:
        async with client.aio.live.connect(model=model, config=config) as session:
            print("Session started")
            await session.send_realtime_input(
                text="Say these words out loud, clearly: hello, this is the doorbell test."
            )
            print("text prompt sent, receiving audio...")
            async def recv_loop():
                async for response in session.receive():
                    sc = getattr(response,'server_content',None)
                    if not sc: continue
                    ot = getattr(sc,'output_transcription',None)
                    if ot and ot.text: transcripts.append(ot.text)
                    mt = getattr(sc,'model_turn',None)
                    if mt:
                        for part in (mt.parts or []):
                            if getattr(part,'inline_data',None) and part.inline_data.data:
                                audio_out.extend(part.inline_data.data)
                    if getattr(sc,'turn_complete',False):
                        print("turn complete"); return
            try:
                await asyncio.wait_for(recv_loop(), timeout=15)
            except asyncio.TimeoutError:
                print("recv timeout 15s")
    except Exception as e:
        print(f"CONNECT/RECV ERROR: {type(e).__name__}: {e}")
        return 2

    print("transcript:", transcripts)
    if audio_out:
        with open(os.path.expanduser('~/doorman/out.raw'),'wb') as f:
            f.write(bytes(audio_out))
        print(f"SAVED {len(audio_out)} bytes -> out.raw")
        # Gemini Live native audio default sample rate is 24kHz pcm16; write wav
        for rate in (24000,16000):
            try:
                with wave.open(os.path.expanduser('~/doorman/out.wav'),'wb') as w:
                    w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate)
                    w.writeframes(bytes(audio_out))
                print(f"wrote out.wav at {rate}Hz"); break
            except Exception as e:
                print('wav err', e)
    else:
        print("NO audio returned")
    return 0

if __name__=='__main__':
    sys.exit(asyncio.run(main()))
