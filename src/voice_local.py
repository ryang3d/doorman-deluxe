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


# ---------------------------------------------------------------- HTTP clients
async def transcribe_utterance(pcm16_16k: bytes, cfg) -> dict:
    """POST a raw 16k s16le utterance to the parakeet service as a WAV file.
    Returns {'text': str, 'language': str}. Raises on non-200."""
    import aiohttp
    buf = io.BytesIO()
    with wave.open(buf, 'wb') as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(SAMPLE_RATE)
        w.writeframes(pcm16_16k)
    buf.seek(0)
    url = cfg['DOORMAN_STT_BASE_URL'].rstrip('/') + '/transcribe'
    data = aiohttp.FormData()
    data.add_field('file', buf, filename='utterance.wav',
                   content_type='audio/wav')
    async with aiohttp.ClientSession() as s:
        async with s.post(url, data=data, timeout=aiohttp.ClientTimeout(total=20)) as r:
            if r.status != 200:
                raise RuntimeError('stt %s: %s' % (r.status, (await r.text())[:200]))
            return await r.json()


async def _voicebox_generate(text, profile_id, engine, cfg,
                             poll_timeout=60.0, poll_step=0.4, poll_cap=2.0) -> dict:
    """POST /generate to voicebox, poll /generate/<id>/status until completed.
    voicebox is async: POST returns a generation record with status 'generating';
    the status endpoint emits SSE lines 'data: {...}' with the final record.
    Polling uses ADAPTIVE backoff — start at 0.4s and double each miss up to
    poll_cap (2.0s). A ~2.5s clip is caught within ~0.4+0.8+1.6 = 2.8s instead of
    the old fixed-2.0s step, which reported a 2.5s clip at ~4.0s (up to ~1.5s of
    pure poll-granularity latency). Returns the completed generation record."""
    import aiohttp, json as _json
    base = cfg['DOORMAN_TTS_BASE_URL'].rstrip('/')
    payload = {"text": text, "language": "en" if engine != 'chatterbox' else "es",
               "profile_id": profile_id, "engine": engine}
    async with aiohttp.ClientSession() as s:
        async with s.post(base + '/generate', json=payload,
                          timeout=aiohttp.ClientTimeout(total=30)) as r:
            if r.status != 200:
                raise RuntimeError('tts post %s: %s' % (r.status, (await r.text())[:200]))
            gid = (await r.json())['id']
        deadline = time.time() + poll_timeout
        step = poll_step
        while time.time() < deadline:
            await asyncio.sleep(step)
            step = min(step * 2, poll_cap)
            async with s.get('%s/generate/%s/status' % (base, gid),
                             timeout=aiohttp.ClientTimeout(total=10)) as r:
                raw = (await r.text()).strip()
            # SSE: possibly multiple 'data: ' lines; take the LAST one
            rec = None
            for line in reversed(raw.splitlines()):
                if line.startswith('data: '):
                    rec = _json.loads(line[6:]); break
            if rec is None:
                raise RuntimeError('tts: bad status payload: %s' % raw[:120])
            st = rec.get('status')
            if st == 'completed':
                return rec
            if st in ('failed', 'cancelled'):
                raise RuntimeError('tts %s: %s' % (st, rec.get('error') or ''))
    raise RuntimeError('tts: poll timeout for %s' % gid)


def _wav_to_pcm16(wav_bytes: bytes, target_rate: int = 24000) -> bytes:
    """Decode a WAV to target_rate Hz mono s16le bytes. voicebox returns 24k mono
    s16 (the rate GeminiAudioTrack expects) so this is usually a pass-through; it
    resamples/converts otherwise. Returns b'' on failure."""
    import numpy as np
    try:
        buf = io.BytesIO(wav_bytes)
        with wave.open(buf, 'rb') as w:
            ch = w.getnchannels(); rate = w.getframerate()
            width = w.getsampwidth(); frames = w.readframes(w.getnframes())
        if width == 2:
            pcm = np.frombuffer(frames, dtype=np.int16).astype(np.float32)
        elif width == 1:
            pcm = (np.frombuffer(frames, dtype=np.uint8).astype(np.float32) - 128.0) * 256.0
        elif width == 4:
            pcm = np.frombuffer(frames, dtype=np.int32).astype(np.float32) / 32768.0
        else:
            return b''
        if ch > 1:
            pcm = pcm.reshape(-1, ch).mean(axis=1)   # downmix to mono
        if rate != target_rate:
            n = int(len(pcm) * target_rate / rate)
            idx = np.linspace(0, len(pcm) - 1, n)
            pcm = np.interp(idx, np.arange(len(pcm)), pcm)
        pcm = np.clip(pcm, -32767, 32767).astype(np.int16)
        return pcm.tobytes()
    except Exception as e:
        log.warning("tts wav decode failed: %s", e)
        return b''


async def synthesize(text, cfg, audio_q, speaking, activity=None):
    """TTS one reply via voicebox/Chatterbox (whole-clip, 24k wav) and queue the
    pcm16 into audio_q, marking speaking state so the mic echo-gate mutes while
    audio plays. Language routing is decided by the CALLER via
    tts_profile_for_language() (pass the language as `text_lang` through cfg-free
    args here: we pick the engine from cfg defaults when no language is given).
    Returns bytes queued."""
    import aiohttp
    profile, engine = tts_profile_for_language(getattr(speaking, '_lang', ''), cfg)
    rec = await _voicebox_generate(text, profile, engine, cfg)
    gid = rec['id']
    # voicebox writes the wav to its generations volume; fetch it via the
    # confirmed REST endpoint GET /audio/{generation_id} (verified 2026-09-15
    # against the live openapi.json).
    base = cfg['DOORMAN_TTS_BASE_URL'].rstrip('/')
    async with aiohttp.ClientSession() as s:
        async with s.get(base + '/audio/' + gid,
                         timeout=aiohttp.ClientTimeout(total=30)) as r:
            if r.status != 200:
                raise RuntimeError('tts audio %s: %s' % (r.status, (await r.text())[:200]))
            wav = await r.read()
    pcm = _wav_to_pcm16(wav)
    if pcm:
        chunk = 24000 * 2  # 1 s of 24k s16le per queue item; pacing is downstream
        for i in range(0, len(pcm) - len(pcm) % chunk, chunk):
            audio_q.put_nowait(pcm[i:i + chunk])
        await speaking.mark_active()
        if activity:
            await activity.mark()
    log.info("tts: queued %d bytes for %d chars (engine=%s)",
             len(pcm), len(text), engine)
    return len(pcm)


async def brain_turn(system_prompt, history, user_text, cfg, activity=None,
                     max_tool_rounds=3):
    """One conversational turn against the OpenAI-compatible endpoint WITH tools
    (default SGLang qwen3.8-27b on the LLM host; Ollama works via the same shape).
    history: list of OpenAI messages (system first, NOT included here).
    Returns (final_text, updated_history). Tools execute via doorman_tools."""
    import aiohttp
    import doorman_tools
    url = cfg['DOORMAN_LLM_BASE_URL'].rstrip('/') + '/chat/completions'
    headers = {'Content-Type': 'application/json'}
    key = cfg.get('DOORMAN_LLM_API_KEY') or ''
    if key and key != 'ollama':
        headers['Authorization'] = 'Bearer ' + key
    messages = [{'role': 'system', 'content': system_prompt}] + history + \
               [{'role': 'user', 'content': user_text}]
    new_msgs = []
    for _round in range(max_tool_rounds + 1):
        payload = {
            'model': cfg['DOORMAN_LLM_MODEL'],
            'messages': messages,
            'tools': openai_tools_schema(),
            'tool_choice': 'auto',
            'max_tokens': 300,
            'stream': False,
            # 'options' (think/keep_alive) is Ollama-only; SGLang ignores unknown
            # top-level keys, so send it only for the Ollama fallback.
        }
        if (cfg.get('DOORMAN_LLM_API_KEY') or '') == 'ollama':
            payload['options'] = {'think': False,
                                  'keep_alive': cfg.get('DOORMAN_LLM_KEEP_ALIVE', '30m')}
        async with aiohttp.ClientSession() as s:
            async with s.post(url, json=payload, headers=headers,
                              timeout=aiohttp.ClientTimeout(total=60)) as r:
                if r.status != 200:
                    raise RuntimeError('llm %s: %s' % (r.status, (await r.text())[:200]))
                d = await r.json()
        choice = d['choices'][0]['message']
        tool_calls = choice.get('tool_calls') or []
        if not tool_calls:
            final = (choice.get('content') or '').strip()
            messages.append({'role': 'assistant', 'content': final})
            new_msgs.append(messages[-1])
            return final, new_msgs
        # execute tools, feed results back
        messages.append(choice)
        for tc in tool_calls:
            fn = tc['function']['name']
            try:
                args = json.loads(tc['function'].get('arguments') or '{}')
            except json.JSONDecodeError:
                args = {}
            log.info("[tool call] %s %s", fn, json.dumps(args)[:200])
            if activity:
                await activity.mark()
            if fn == 'notify_ryan':
                ok, val = await doorman_tools.notify_ryan(
                    str(args.get('message', '')))
            elif fn == 'snapshot_front_door':
                ok, val = await doorman_tools.snapshot_front_door()
            else:
                ok, val = False, 'unknown tool ' + fn
            messages.append({'role': 'tool',
                             'tool_call_id': tc.get('id', fn),
                             'content': json.dumps({'ok': ok, 'value': val})})
            new_msgs.append(messages[-1])
    raise RuntimeError('brain: too many tool rounds')


# ---------------------------------------------------------------- full interaction
async def run_interaction_local(system_prompt, trigger_text,
                                duration_s=120.0, idle_timeout_s=None):
    """Local-engine door interaction. Reuses the proven talkback WebRTC transport and
    teardown from audio_bridge; replaces the Gemini Live loop with:
    mic(ffmpeg RTSP) -> webrtcvad endpointer -> parakeet STT (3080 Ti, language
    detection) -> LLM brain (SGLang qwen3.8-27b on the LLM host, +tools) ->
    voicebox/Chatterbox TTS (Ryan G clone) -> audio_q -> GeminiAudioTrack ->
    go2rtc -> doorbell speaker. TTS engine follows the STT-detected language:
    EN -> chatterbox_turbo (clone), ES/other -> chatterbox multilingual."""
    import asyncio
    import audio_bridge as ab
    cfg = load_cfg()
    import doorman as _d   # ActivityClock lives there; avoid import cycle at module top
    activity = _d.ActivityClock()
    audio_q = asyncio.Queue()
    stop_ev = asyncio.Event()
    sysp = local_system_prompt(system_prompt)

    # --- talkback connect (retry loop IDENTICAL to gemini path) ---
    pc = ws = mic = keep_task = recv_holder = None
    for attempt in range(3):
        try:
            pc, ws, mic, keep_task, recv_holder = await ab.talkback_connect(cfg, audio_q)
            break
        except Exception as e:
            log.warning("local talkback attempt %d/3 failed: %s", attempt + 1,
                        (str(e).splitlines()[0] if str(e) else e)[:120])
            if attempt < 2:
                await asyncio.sleep(8.0)
    if ws is None:
        log.error("local talkback connect failed after 3 attempts")
        return 2

    speaking = ab.SpeakingState()
    ep = UtteranceEndpointer(
        silence_ms=int(cfg.get('DOORMAN_LOCAL_SILENCE_MS', 700)),
        min_speech_ms=int(cfg.get('DOORMAN_LOCAL_MIN_SPEECH_MS', 300)))
    history = []

    async def mic_loop():
        """Same ffmpeg RTSP open + probe + retry as ab.mic_to_gemini, but frames feed
        the endpointer instead of a Gemini session."""
        mic_rtsp = cfg.get('DOORMAN_MIC_RTSP') or cfg.get('CAM_MIC_RTSP')
        proc = await ab.open_mic_ffmpeg(mic_rtsp)
        if proc is None:
            log.warning("local mic: RTSP audio failed to open")
            return
        acc = bytearray()
        try:
            while not stop_ev.is_set():
                data = await proc.stdout.read(3200)
                if not data:
                    break
                acc += data
                while len(acc) >= VAD_FRAME_BYTES:
                    frame = bytes(acc[:VAD_FRAME_BYTES])
                    del acc[:VAD_FRAME_BYTES]
                    if await speaking.muted():
                        ep.push_frame(frame, muted=True)
                        continue
                    utter = ep.push_frame(frame, muted=False)
                    if utter:
                        await handle_utterance(utter)
        except asyncio.CancelledError:
            pass
        finally:
            try: proc.kill()
            except Exception: pass

    async def handle_utterance(pcm16: bytes):
        await activity.mark()
        log.info("local: endpoint, %d ms of audio", len(pcm16) // (SAMPLE_RATE // 500))
        try:
            stt = await transcribe_utterance(pcm16, cfg)
        except Exception as e:
            log.warning("local stt failed: %s", e)
            return
        text = (stt.get('text') or '').strip()
        if not text:
            log.info("local stt: silence/empty")
            return
        # Parakeet v3 does NOT label the language (its NeMo Hypothesis has no
        # language field — confirmed 2026-09-16), so STT 'language' is always
        # empty. The language is taken from DOORMAN_LOCAL_LANG_FALLBACK
        # ('auto' -> 'en' engine default; 'es' -> 'es' engine for an all-Spanish
        # household). Per-utterance switching needs a language detector
        # (out of scope for the 2-language scope).
        lang = (stt.get('language') or '').strip() \
               or (cfg.get('DOORMAN_LOCAL_LANG_FALLBACK', 'auto') or 'en')
        if lang.lower() in ('auto', 'default'):
            lang = 'en'
        speaking._lang = lang            # TTS engine follows the chosen language
        log.info("[visitor said] %s (%s)", text, lang)
        try:
            reply, new_hist = await brain_turn(sysp, history, text, cfg,
                                               activity=activity)
            history.extend(new_hist)
            if len(history) > 24:      # cap: keep the recent window
                del history[:-24]
        except Exception as e:
            log.warning("local brain failed: %s", e)
            return
        if not reply:
            return
        log.info("[doorman said] %s", reply[:120])
        try:
            await synthesize(reply, cfg, audio_q, speaking, activity=activity)
        except Exception as e:
            log.warning("local tts failed: %s", e)

    mic_task = asyncio.create_task(mic_loop())
    log.info("local interaction starting (max %ss): %s",
             duration_s, trigger_text[:80])
    # Prime: the trigger text IS the first brain user turn (greeting). EN clone.
    speaking._lang = 'en'
    try:
        reply, new_hist = await brain_turn(sysp, history, trigger_text, cfg,
                                           activity=activity)
        history.extend(new_hist)
        if reply:
            await synthesize(reply, cfg, audio_q, speaking, activity=activity)
    except Exception as e:
        log.warning("local prime failed: %s", e)

    # --- watchdog + teardown: IDENTICAL structure to the gemini path ---
    import time
    interaction_start = time.monotonic()
    try:
        if idle_timeout_s:
            while not stop_ev.is_set():
                idle = await activity.idle_seconds()
                if idle >= idle_timeout_s:
                    log.info("idle for %.0fs >= %ss, ending local interaction",
                             idle, idle_timeout_s)
                    break
                if (time.monotonic() - interaction_start) >= duration_s:
                    break
                await asyncio.sleep(0.5)
        else:
            await asyncio.wait_for(stop_ev.wait(), timeout=duration_s)
    except asyncio.TimeoutError:
        pass
    log.info("ending local interaction")
    mic_task.cancel()
    try:
        await asyncio.wait_for(mic_task, timeout=2)
    except Exception:
        pass
    try:
        end = asyncio.get_event_loop().time() + 0.3
        while asyncio.get_event_loop().time() < end:
            await asyncio.sleep(0.02)
    except Exception:
        pass
    try:
        await pc.close()
    except Exception:
        pass
    await asyncio.sleep(0.8)
    keep_task.cancel()
    try:
        await asyncio.wait_for(keep_task, timeout=1)
    except Exception:
        pass
    try:
        await ws.close()
    except Exception:
        pass
    return 0
