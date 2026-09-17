#!/usr/bin/env python3
"""Local voice engine for Doorman (DOORMAN_VOICE_ENGINE=local).

STT:  Parakeet TDT 0.6B v3 service  -> POST {DOORMAN_STT_BASE_URL}/transcribe
Brain: OpenAI-compatible /chat/completions (default SGLang qwen3.8-27b on
       the LLM host; fallback Ollama qwen3.5:9b on the doorman host)
TTS:  Voicebox /generate (Chatterbox, Ryan's clone)
      EN -> engine=chatterbox_turbo,  es* -> engine=chatterbox (Multilingual)
Audio in/out formats: visitor mic 16k s16le (ffmpeg pipeline), AI audio 24k s16le
queued into audio_q for the existing GeminiAudioTrack (no rate conversion needed).
"""
import asyncio, base64, io, json, logging, time, wave
import numpy as np

import doorman_config as _dc

log = logging.getLogger("doorman.local")

SAMPLE_RATE = 16000          # mic / VAD / STT rate
VAD_FRAME_MS = 30            # webrtcvad needs 10/20/30 ms frames
VAD_FRAME_BYTES = SAMPLE_RATE * VAD_FRAME_MS // 1000 * 2  # s16le bytes


def load_cfg():
    return _dc.load()


# ---------------------------------------------------------------- language -> tts
def tts_profile_for_language(language: str, cfg) -> tuple:
    """Pick the Voicebox (profile, engine) from the STT-detected language code.
    Any 'es*' -> Spanish engine (chatterbox Multilingual); everything else (incl.
    unknown/empty) -> the English engine (chatterbox_turbo). Both use Ryan's cloned
    voice profile. Returns (profile_id, engine)."""
    lang = (language or '').strip().lower()
    if lang.startswith('es'):
        return (cfg.get('DOORMAN_TTS_PROFILE_ES') or cfg.get('DOORMAN_TTS_PROFILE_EN'),
                cfg.get('DOORMAN_TTS_ENGINE_ES') or 'chatterbox')
    return (cfg.get('DOORMAN_TTS_PROFILE_EN') or 'ffadb2a2-cacc-4c7f-8d26-69f7c4c22246',
            cfg.get('DOORMAN_TTS_ENGINE_EN') or 'chatterbox_turbo')


# ---------------------------------------------------------------- VAD endpointer
class UtteranceEndpointer:
    """Feeds 30 ms s16le 16k frames; detects one utterance:
    pre-roll (last 300 ms of silence kept) + speech + end-silence.

    State machine: IDLE -> (voice for >= min_speech_ms) SPEAKING ->
    (silence >= silence_ms) -> on_endpoint() with the buffered utterance, back to IDLE.
    While `muted` (AI is speaking), frames are ignored (echo gate) but pre-roll resets.
    All methods are sync; caller runs inside an async task at 30 ms cadence.
    """

    def __init__(self, silence_ms=700, min_speech_ms=300, preres_ms=300):
        import webrtcvad
        self.vad = webrtcvad.Vad(3)   # 0-3 aggressiveness; 3 = drop non-speech hard
        self.silence_ms = int(silence_ms)
        self.min_speech_ms = int(min_speech_ms)
        self.frames = []              # ring of (frame_bytes, is_speech, muted)
        self.keep_frames = max(silence_ms + preres_ms + 100, 0) // VAD_FRAME_MS + 1

    def push_frame(self, frame: bytes, muted: bool):
        """One 30 ms frame. Returns the utterance bytes (s16le 16k) when an endpoint
        fires, else None."""
        if len(frame) != VAD_FRAME_BYTES:
            frame = frame[:VAD_FRAME_BYTES]
            if len(frame) != VAD_FRAME_BYTES:
                return None
        try:
            speech = (not muted) and self.vad.is_speech(frame, SAMPLE_RATE)
        except Exception:
            speech = False
        self.frames.append((frame, speech, muted))
        if len(self.frames) > self.keep_frames:
            self.frames = self.frames[-self.keep_frames:]
        return self._check_endpoint()

    def _check_endpoint(self):
        """Find the most recent: contiguous voice >= min_speech followed by contiguous
        silence >= silence_ms (both in the UNMUTED region after the voice burst)."""
        n = len(self.frames)
        if n < (self.silence_ms + self.min_speech_ms) // VAD_FRAME_MS:
            return None
        # scan backwards from the newest frame for the end-silence run
        i = n - 1
        silence = 0
        while i >= 0:
            fr, sp, muted = self.frames[i]
            if muted or sp:
                break
            silence += VAD_FRAME_MS
            i -= 1
        if silence < self.silence_ms:
            return None
        j = i
        voice = 0
        while j >= 0:
            fr, sp, muted = self.frames[j]
            if muted or not sp:
                break
            voice += VAD_FRAME_MS
            j -= 1
        if voice < self.min_speech_ms:
            return None
        # utterance = from 300 ms before the voice start through the voice end.
        start = j
        preres = self.frames[start - 10:start] if start >= 10 else self.frames[:start]
        end = i + 1
        utter = b''.join(fr for fr, _, _ in self.frames[start:end])
        self.frames = self.frames[end:]   # consume; keep post-end silence for pre-roll
        return utter or None

    def flush(self):
        """Discard buffered frames (call between turns)."""
        self.frames = []


def openai_tools_schema():
    return [
        {"type": "function", "function": {
            "name": "snapshot_front_door",
            "description": ("Capture a still of the front door camera and save it. "
                            "Use when you need to see who/what is at the door. "
                            "Returns the saved file path."),
            "parameters": {"type": "object", "properties": {}, "required": []}}},
        {"type": "function", "function": {
            "name": "notify_ryan",
            "description": ("Send a notification to the homeowner about a package, "
                            "issue or urgent report. Provide a concise message."),
            "parameters": {"type": "object",
                           "properties": {"message": {"type": "string"}},
                           "required": ["message"]}}},
    ]


def local_system_prompt(base_prompt: str) -> str:
    return base_prompt + (
        "\n\nLOCAL-PIPELINE NOTES: You are now spoken by a local text-to-speech engine. "
        "Keep replies short: one or two sentences, no lists, no markdown. Reply in the "
        "same language the visitor uses (English or Spanish). When a tool is available "
        "and the situation calls for it, call the tool FIRST, then say one short line.")
