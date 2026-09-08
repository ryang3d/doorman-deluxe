#!/usr/bin/env python3
"""FIX VERIFICATION: run the FULL run_once but with mic_to_gemini's blocking stdout.read
offloaded to a thread (asyncio.to_thread). Poll go2rtc consumers every 2s — must STAY >=1
while Gemini speaks. Also thread-safe wrapper uses threading instead of queue-unsafe reads."""
import asyncio, json, os, sys, logging, time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
import audio_bridge as ab
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s %(message)s', datefmt='%H:%M:%S')
import aiohttp

# ---- patched mic_to_gemini: read offloaded to a thread ----
_orig_mic = ab.mic_to_gemini
async def mic_to_gemini_fixed(session, stop_ev, speaking, sample_bytes=3200):
    import subprocess, threading
    cmd=['ffmpeg','-hide_banner','-loglevel','error','-rtsp_transport','tcp',
         '-i',ab.CAM_MIC_RTSP,'-f','s16le','-acodec','pcm_s16le','-ar','16000','-ac','1','pipe:1']
    log = ab.log
    log.info("starting mic capture (THREADED read): %s", ab.CAM_MIC_RTSP.split('@')[1])
    proc=subprocess.Popen(cmd,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    def drain():
        for line in proc.stderr: pass
    threading.Thread(target=drain,daemon=True).start()
    from google.genai import types
    try:
        while not stop_ev.is_set():
            # NON-BLOCKING on the event loop:
            data = await asyncio.to_thread(proc.stdout.read, sample_bytes)
            if not data:
                log.warning("mic capture ended (ffmpeg closed)")
                break
            if len(data)==sample_bytes:
                if await speaking.muted():
                    continue
                try:
                    await session.send_realtime_input(audio=types.Blob(data=data, mime_type="audio/pcm;rate=16000"))
                except Exception as e:
                    log.warning("send_realtime_input err: %s", e)
                    break
    finally:
        try: proc.terminate()
        except Exception: pass
        log.info("mic capture stopped")

async def poll_consumers(cfg, stop):
    base=cfg['FRIGATE_URL'].rstrip('/')
    async with aiohttp.ClientSession() as s:
        t=0
        while not stop.is_set():
            try:
                async with s.get(f"{base}/api/go2rtc/api/streams?src=front_doorbell_twoway") as r:
                    d=await r.json()
                c=len(d.get('consumers') or [])
                print(f"[POLL t={t}s] consumers={c}", flush=True)
            except Exception as e:
                print(f"[POLL t={t}s] ERR {e}", flush=True)
            t+=2
            try: await asyncio.wait_for(stop.wait(),timeout=2)
            except asyncio.TimeoutError: pass

async def main():
    cfg=ab.load_config()
    ab.mic_to_gemini = mic_to_gemini_fixed   # apply fix
    stop=asyncio.Event()
    p=asyncio.create_task(poll_consumers(cfg, stop))
    try:
        await asyncio.wait_for(ab.run_once(20, ab.DEFAULT_PROMPT), timeout=60)
    except asyncio.TimeoutError:
        print("run_once timed out")
    finally:
        stop.set()
        p.cancel()

asyncio.run(main())
