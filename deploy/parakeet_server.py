#!/usr/bin/env python3
"""Parakeet TDT 0.6B v3 STT service. POST /transcribe (multipart 16k mono WAV) ->
JSON {"text": str, "language": str, "seconds": float}. GPU if available, else CPU.
Model is loaded once at startup; /health returns ready after warmup."""
import io, json, logging, os, time, wave, tempfile, subprocess, base64
import fastapi, uvicorn
from fastapi import File, UploadFile

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("parakeet")
MODEL_NAME = os.environ.get("PARAKEET_MODEL", "nvidia/parakeet-tdt-0.6b-v3")

import torch
import nemo.collections.asr as nemo_asr

DEVICE = "cuda" if torch.cuda.is_available() else "cpu"
log.info("loading %s on %s ...", MODEL_NAME, DEVICE)
MODEL = nemo_asr.models.ASRModel.from_pretrained(model_name=MODEL_NAME)
MODEL = MODEL.to(DEVICE).eval()

def health():
    return {"ok": True, "device": DEVICE, "model": MODEL_NAME}

def _to_16k_mono_wav(data: bytes) -> str:
    """Return a path to a 16kHz mono s16 WAV, transcoding from any wav/pcm input."""
    import hashlib
    raw = tempfile.mktemp(suffix='.raw')
    wav = tempfile.mktemp(suffix='.wav')
    with open(raw, 'wb') as f: f.write(data)
    # If it's already a 16k mono wav, skip ffmpeg (fast path for our own uploads).
    try:
        with wave.open(io.BytesIO(data)) as w:
            if w.getframerate() == 16000 and w.getnchannels() == 1 and w.getsampwidth() == 2:
                with open(wav, 'wb') as out:
                    out.write(data)
                os.unlink(raw)
                return wav
    except Exception:
        pass
    subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-i', raw,
                    '-ar', '16000', '-ac', '1', '-c:a', 'pcm_s16le', wav], check=True)
    os.unlink(raw)
    return wav

async def transcribe(file: UploadFile = File(...)):
    data = await file.read()
    wav_path = _to_16k_mono_wav(data)
    try:
        t0 = time.time()
        out = MODEL.transcribe([wav_path], batch_size=1)
        sample = out[0]
        text = getattr(sample, 'text', '').strip()
        # CONFIRMED 2026-09-16: nvidia/parakeet-tdt-0.6b-v3 is an EncDecRNNTBPEModel;
        # its NeMo transcribe() returns a Hypothesis with fields
        # [text, words, alignments, tokens, score, timestamp, length, y, y_sequence,
        #  token_confidence, word_confidence, ...] but NO language field. The model is
        # multilingual (transcribes ES correctly) but does not label the language.
        # `language` is therefore empty by design; the doorman layer's
        # tts_profile_for_language() defaults empty -> EN engine, and the brain's
        # system prompt (Task 4c / 6) tells it which language to reply in.
        lang = str(getattr(sample, 'language', '') or '').strip()
        return {"text": text, "language": lang, "seconds": round(time.time() - t0, 3)}
    finally:
        try: os.unlink(wav_path)
        except OSError: pass

def _make_app():
    app = fastapi.FastAPI()
    app.get('/health')(health)
    app.post('/transcribe')(transcribe)
    return app

if __name__ == '__main__':
    uvicorn.run(_make_app(), host='0.0.0.0', port=10301)
