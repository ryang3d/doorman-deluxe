#!/usr/bin/env python3
"""faster-whisper STT service. Drop-in replacement for parakeet_server.py.

Contract (identical to the Parakeet service so the doorman's transcribe_utterance
needs no changes):
    POST /transcribe  (multipart field `file`, any wav/pcm) -> {"text","language","seconds"}
    GET  /health      -> {"ok":true,"device","model"}

Differences vs Parakeet that help the door use case:
  - CTranslate2/Whisper is far more robust on noisy far-field audio and short clips,
    and returns real confidence so the caller can decide on empty/low-conf retries.
  - Language is actually detected (Whisper auto-detects from the audio), so a
    Spanish visitor can route the TTS engine correctly without extra config.
    Set WHISPER_DETECT_LANG=false to return an empty language (old Parakeet behaviour)
    and let the doorman fall back to its DOORMAN_LOCAL_LANG_FALLBACK instead.

Config via env:
    WHISPER_MODEL      'base.en' | 'small.en' | 'medium.en' | 'small' | 'medium' ... (default 'small.en')
    WHISPORT           listen port (default 10302 so it coexists with Parakeet on 10301 for A/B)
    WHISPER_DEVICE     'cuda' | 'cpu' (default: auto -> cuda if available)
    WHISPER_COMPUTE    'float16' | 'int8_float16' | 'int8' | 'float32' (default auto: fp16 on cuda, int8 on cpu)
    WHISPER_DETECT_LANG default 'true'
    WHISPER_BEAM_SIZE  default 5
    WHISPER_INITIAL_PROMPT  optional text biasing the model toward a vocabulary
"""
import io, logging, os, time, wave, tempfile, subprocess
import fastapi, uvicorn
from fastapi import File, UploadFile

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("whisper")

MODEL_NAME = os.environ.get("WHISPER_MODEL", "small.en")
PORT = int(os.environ.get("WHISPORT", "10302"))
DETECT_LANG = os.environ.get("WHISPER_DETECT_LANG", "true").lower() in ("1", "true", "yes", "on")
BEAM_SIZE = int(os.environ.get("WHISPER_BEAM_SIZE", "5"))
INITIAL_PROMPT = os.environ.get("WHISPER_INITIAL_PROMPT", "") or None
# VAD filter (Silero): when True, transcribe() drops clips with no detected speech,
# which is what stops Whisper hallucinating phrases on door ambient noise. Default on.
VAD_FILTER = os.environ.get("WHISPER_VAD_FILTER", "true").lower() in ("1", "true", "yes", "on")

DEVICE = os.environ.get("WHISPER_DEVICE", "").lower()
if DEVICE not in ("cuda", "cpu"):
    try:
        from ctranslate2 import get_cuda_device_count
        DEVICE = "cuda" if get_cuda_device_count() > 0 else "cpu"
    except Exception:
        DEVICE = "cpu"
# float16 on GPU; int8 on CPU (ctranslate2 int8 needs no extra deps)
COMPUTE = os.environ.get("WHISPER_COMPUTE", "")
if not COMPUTE:
    COMPUTE = "float16" if DEVICE == "cuda" else "int8"

log.info("loading faster-whisper %s on %s (%s) ...", MODEL_NAME, DEVICE, COMPUTE)
from faster_whisper import WhisperModel
MODEL = WhisperModel(MODEL_NAME, device=DEVICE, compute_type=COMPUTE)

def _warmup():
    """Run one tiny transcribe so ctranslate2 loads its kernels / allocates the
    first inference buffer; otherwise the real first door utterance pays that cost."""
    try:
        import wave as _w, struct
        p = tempfile.mktemp(suffix=".wav")
        with _w.open(p, "wb") as out:
            out.setnchannels(1); out.setsampwidth(2); out.setframerate(16000)
            out.writeframes(struct.pack("<" + "h" * 16000, *([0] * 16000)))  # 1.0 s silence
        MODEL.transcribe(p, language=None, beam_size=1)
        os.unlink(p)
        log.info("whisper warmup done")
    except Exception as e:
        log.warning("whisper warmup skipped: %s", e)

_warmup()

def health():
    return {"ok": True, "device": DEVICE, "model": MODEL_NAME, "engine": "faster-whisper"}

def _to_16k_mono_wav(data: bytes) -> str:
    """Return a path to a 16kHz mono s16 WAV, transcoding from any wav/pcm input."""
    wav = tempfile.mktemp(suffix=".wav")
    # Fast path: already a 16k mono s16 wav (our own uploads).
    try:
        with wave.open(io.BytesIO(data)) as w:
            if w.getframerate() == 16000 and w.getnchannels() == 1 and w.getsampwidth() == 2:
                with open(wav, "wb") as out:
                    out.write(data)
                return wav
    except Exception:
        pass
    raw = tempfile.mktemp(suffix=".raw")
    with open(raw, "wb") as f:
        f.write(data)
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", raw,
                    "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", wav], check=True)
    os.unlink(raw)
    return wav

async def transcribe(file: UploadFile = File(...)):
    data = await file.read()
    wav_path = _to_16k_mono_wav(data)
    try:
        t0 = time.time()
        lang_arg = None if DETECT_LANG else "en"
        segments, info = MODEL.transcribe(
            wav_path,
            language=lang_arg,
            beam_size=BEAM_SIZE,
            vad_filter=VAD_FILTER,
            vad_parameters={"min_silence_duration_ms": 1500, "threshold": 0.5}
                           if VAD_FILTER else None,
            condition_on_previous_text=False,  # avoid loop/echo of repeated greetings
            initial_prompt=INITIAL_PROMPT,
        )
        # consume the generator; collect per-segment no-speech-prob + avg-logprob
        parts = []
        nsp_list, alp_list, lang_list = [], [], []
        for seg in segments:
            s = seg.text.strip()
            if s:
                parts.append(s)
                try: nsp_list.append(float(seg.no_speech_prob))
                except Exception: pass
                try: alp_list.append(float(seg.avg_logprob))
                except Exception: pass
                try: lang_list.append(seg.language or "")
                except Exception: pass
        text = " ".join(parts).strip()
        seconds = round(time.time() - t0, 3)
        lang = (info.language or "") if DETECT_LANG else ""
        # Max no_speech_prob over kept segments = how "silence-like" the kept
        # content is. Higher -> more likely a hallucination. Callers can gate.
        max_nsp = round(max(nsp_list), 4) if nsp_list else None
        # mean per-word logprob as a rough confidence in [0,1]
        conf = None
        if alp_list:
            import math
            conf = round(sum(math.exp(min(a, 0.0)) for a in alp_list) / len(alp_list), 4)
        return {"text": text, "language": lang, "seconds": seconds,
                "language_probability": round(getattr(info, "language_probability", 0.0), 3),
                "no_speech_prob": max_nsp, "confidence": conf}
    finally:
        try:
            os.unlink(wav_path)
        except OSError:
            pass

def _make_app():
    app = fastapi.FastAPI()
    app.get("/health")(health)
    app.post("/transcribe")(transcribe)
    return app

if __name__ == "__main__":
    log.info("whisper STT listening on :%d (model=%s device=%s)", PORT, MODEL_NAME, DEVICE)
    uvicorn.run(_make_app(), host="0.0.0.0", port=PORT)
