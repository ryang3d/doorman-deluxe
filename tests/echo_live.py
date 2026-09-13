#!/usr/bin/env python3
"""Phase 1 Task 1.3: minimal Gemini Live echo test from a wav file.
Reads a wav, sends it to Gemini Live, captures transcript + audio reply.
Verifies the model string + API work before wiring hardware."""
import asyncio, os, sys, subprocess, wave

def load_key():
    for line in open(os.path.expanduser('~/.hermes/profiles/home-admin/frigate.env')):
        if line.startswith('GEMINI_API_KEY='):
            return line.split('=',1)[1].strip().strip('"').strip("'")
    return None

async def main():
    key = load_key()
    if not key:
        print("NO GEMINI_API_KEY in frigate.env"); return 1
    os.environ['GEMINI_API_KEY'] = key

    from google import genai
    from google.genai import types

    wav_path = sys.argv[1] if len(sys.argv)>1 else None
    if not wav_path:
        print("usage: echo_live.py <input.wav>"); return 1

    # ensure 16kHz mono pcm16 wav
    tmp='/tmp/echo_input.wav'
    subprocess.run(['ffmpeg','-y','-loglevel','error','-i',wav_path,'-ac','1','-ar','16000','-c:a','pcm_s16le',tmp],check=True)
    with wave.open(tmp,'rb') as w:
        frames=w.readframes(w.getnframes())
    print(f"input: {len(frames)} bytes pcm16 16k mono")

    model = "gemini-3.1-flash-live-preview"
    config = {"response_modalities":["AUDIO"]}

    print(f"connecting {model} ...")
    client = genai.Client(api_key=key)
    audio_out = bytearray()
    transcript=[]

    async with client.aio.live.connect(model=model, config=config) as session:
        print("Session started")
        await session.send_realtime_input(
            audio=types.Blob(data=frames, mime_type="audio/pcm;rate=16000")
        )
        print("audio sent, receiving...")
        # receive for up to ~12s
        async def recv_loop():
            async for response in session.receive():
                sc = getattr(response, 'server_content', None)
                if sc:
                    if getattr(sc,'input_transcription',None) and sc.input_transcription.text:
                        transcript.append(("user", sc.input_transcription.text))
                    if getattr(sc,'output_transcription',None) and sc.output_transcription.text:
                        transcript.append(("gemini", sc.output_transcription.text))
                    mt = getattr(sc,'model_turn',None)
                    if mt:
                        for part in (mt.parts or []):
                            if getattr(part,'inline_data',None) and part.inline_data.data:
                                audio_out.extend(part.inline_data.data)
                    if getattr(sc,'turn_complete',False):
                        print("turn_complete reached")
                        return
        try:
            await asyncio.wait_for(recv_loop(), timeout=15)
        except asyncio.TimeoutError:
            print("receive timeout after 15s")

    print("=== transcript ===")
    for who,t in transcript:
        print(f"{who}: {t}")
    if audio_out:
        with open(os.path.expanduser('~/doorman/out.raw'),'wb') as f:
            f.write(bytes(audio_out))
        print(f"SAVED {len(audio_out)} bytes audio -> out.raw")
        # Also wrap into a wav assuming L16 24kHz mono (Gemini default) - try common rates
        import wave as wmod
        for rate in (24000,16000):
            try:
                ow=os.path.expanduser('~/doorman/out.wav')
                with wmod.open(ow,'wb') as w:
                    w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate)
                    w.writeframes(bytes(audio_out))
                print(f"wrote {ow} at {rate}Hz")
                break
            except Exception as e:
                print('wav write err', e)
    else:
        print("NO audio returned")
    return 0

if __name__=='__main__':
    sys.exit(asyncio.run(main()))
