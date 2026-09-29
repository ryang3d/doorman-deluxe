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

    def __init__(self, silence_ms=700, min_speech_ms=300, preres_ms=300,
                 aggressiveness=3, max_ring_ms=10000, min_rms=500.0):
        import webrtcvad
        self.vad = webrtcvad.Vad(aggressiveness)  # 0-3; 3 = drop non-speech hard
        self.silence_ms = int(silence_ms)
        self.min_speech_ms = int(min_speech_ms)
        self.min_rms = float(min_rms)
        # Ring must hold a full utterance (up to ~10 s) so it is NOT truncated to
        # its last ~390 ms. The old keep=silence+preres+100 (~1.1 s) chopped every
        # utterance to its tail word, which is why STT only heard "Yeah."/"Oh."
        # on the live door mic. 10 s ring + 48 KB/frame is ~480 KB, cheap in RAM.
        ring_from_silence = max(silence_ms + preres_ms + 100, 0) // VAD_FRAME_MS + 1
        self.keep_frames = max(ring_from_silence,
                               int(max_ring_ms) // VAD_FRAME_MS + 1)
        self.frames = []              # ring of (frame_bytes, is_speech, muted, t_mono)
        self._speech_total = 0        # cumulative count of non-muted speech frames (VAD)
        # Duration (ms) of the currently OPEN trailing speech segment (visitor is
        # mid-utterance, not yet finalized). 0 when no open segment. Used to refresh
        # the idle clock while the visitor is still talking so the watchdog cannot
        # end the session mid-sentence (a 700ms-silence endpointer never finalizes
        # long connected speech, so activity must come from "still speaking" not
        # from finalized STT text).
        self._open_seg_ms = 0

    def open_segment_ms(self):
        """ms of the currently-open trailing VAD speech segment, 0 if none."""
        return self._open_seg_ms

    def speech_frame_count(self):
        """Cumulative number of non-muted frames the VAD flagged as speech.
        Diagnostics only: if this stays 0 while audio is flowing, the VAD
        aggressiveness / mic level / signal is the problem."""
        return self._speech_total

    def push_frame(self, frame: bytes, muted: bool, now: float = None):
        """One 30 ms frame. Returns the utterance bytes (s16le 16k) when an endpoint
        fires, else None. `now` is the monotonic timestamp for this frame (defaults to
        time.monotonic()). Tests pass a synthetic clock to drive the wall-clock gap
        logic deterministically without sleeping."""
        if len(frame) != VAD_FRAME_BYTES:
            frame = frame[:VAD_FRAME_BYTES]
            if len(frame) != VAD_FRAME_BYTES:
                return None
        try:
            speech = (not muted) and self.vad.is_speech(frame, SAMPLE_RATE)
        except Exception:
            speech = False
        if speech:
            self._speech_total += 1
        import time as _time
        if now is None:
            now = _time.monotonic()
        # t_mono: wall-clock stamp. Gap detection in _check_endpoint uses the time
        # DELTA between frames, not frame count, so a mic RTSP stall (no frames for
        # 4-12s) splits a segment instead of letting two utterances merge.
        self.frames.append((frame, speech, muted, now))
        if len(self.frames) > self.keep_frames:
            self.frames = self.frames[-self.keep_frames:]
        return self._check_endpoint(now)

    # Tolerated gap inside a voice burst. A between-words pause in a normal
    # sentence is ~150-300 ms, so 300 ms keeps a whole sentence/utterance as one
    # segment without merging two separate turns. Isolated ambient-noise frames
    # are separated by far more, so they stay their own (too-short) segments.
    VOICE_GAP_MS = 300

    def _check_endpoint(self, now: float = None):
        """Fire when the most recent UNMUTED voice segment is >= min_speech_ms,
        has mean RMS >= min_rms, and >= silence_ms has elapsed since its last frame.

        2026-09-17 rewrite (live door-mic testing). Two findings drove the changes:
        (1) the ring was only ~1.1 s, so it truncated every utterance to its last
        ~390 ms -> STT only heard the tail word ("Yeah." / "Oh."). The ring is now
        ~10 s so a full utterance survives.
        (2) the door mic's ambient noise still flags as VAD "speech" at level 2
        (RMS ~300-400) even though it is far quieter than real voice (RMS 9000+).
        So a segment only counts if its mean RMS >= min_rms (default 500): real
        voice passes, ambient noise blips do not. This is what makes the endpointer
        stop firing on the 390 ms noise blips and instead fire on your actual words.

        2026-09-19: gaps are now measured in WALL-CLOCK time (the t_mono on each
        frame), not frame count. The RTSP mic source stalls for 4-12 s during
        two-way sessions (AD410 concurrent-stream load); during a stall ffmpeg
        delivers no frames, so a frame-count gap of N frames is actually N seconds
        of real time. Measuring by count let two separate utterances (one before a
        stall, one after) be seen as a single ~300 ms gap and MERGED into one
        ~9 s blob that STT hallucinated a mix out of ("Is Jenny home?" -> "Why is
        Jenny on?", 2026-09-19). Wall-clock gaps make a stall split the segment.
        """
        import time as _time
        if now is None:
            now = _time.monotonic()
        n = len(self.frames)
        if n < 2:
            return None
        # Forward scan tracking the most recent UNMUTED voice *segment* that
        # reached min_speech_ms. Segments are delimited by a WALL-CLOCK gap
        # > VOICE_GAP_MS (or by frame count, which is the same when frames arrive
        # on time), by muted (echo-gate) frames, or by end-of-ring.
        gap_s = self.VOICE_GAP_MS / 1000.0
        last_good = None                 # (start, end) of most recent long-enough segment
        seg_start = seg_end = None
        for i in range(n):
            fr, sp, muted, t = self.frames[i]
            if muted:
                # echo-gate region resets the current segment
                seg_start = seg_end = None
                continue
            if sp:
                if seg_start is None:
                    seg_start = seg_end = i
                elif (t - self.frames[seg_end][3]) > gap_s:
                    # wall-clock gap too big -> previous segment closed; record it
                    # if long enough. (A stall shows up here as a big time gap even
                    # though only a few frames exist between the two speech runs.)
                    # 1 ms epsilon: boundary duration vs min_speech_ms must not lose
                    # to float timestamp error.
                    if (self.frames[seg_end][3] - self.frames[seg_start][3]
                            + VAD_FRAME_MS / 1000.0) * 1000 + 1.0 >= self.min_speech_ms:
                        last_good = (seg_start, seg_end)
                    seg_start = seg_end = i
                else:
                    seg_end = i
        # close the trailing (possibly still-open) segment if it's long enough AND
        # it is the most recent long-enough segment (don't overwrite a good
        # segment with a trailing too-short noise blip).
        if seg_start is not None:
            trail_ms = (self.frames[seg_end][3] - self.frames[seg_start][3]
                        + VAD_FRAME_MS / 1000.0) * 1000
            if trail_ms + 1.0 >= self.min_speech_ms:
                last_good = (seg_start, seg_end)
        # Expose the currently-OPEN trailing segment's duration (the visitor is
        # mid-utterance, hasn't hit the 700ms end-silence yet), GATED on the RMS
        # threshold so ambient noise doesn't keep it "open". This door mic's
        # ambient flags as VAD "speech" (RMS ~300-400) so a VAD-only open segment
        # stays open ~80% of the time even with nobody speaking; requiring
        # RMS >= min_rms keeps the idle clock fresh only during real voice
        # (RMS 9000+). 0 when no open RMS-passing speech run.
        if seg_start is not None:
            dur_ms = (self.frames[seg_end][3] - self.frames[seg_start][3]
                      + VAD_FRAME_MS / 1000.0) * 1000
            try:
                import numpy as _np
                ob = b''.join(fr for fr, _, _, _ in self.frames[seg_start:seg_end + 1])
                a = _np.frombuffer(ob, dtype=_np.int16).astype(_np.float64)
                orms = float(_np.sqrt(_np.mean(a ** 2))) if a.size else 0.0
            except Exception:
                orms = 0.0
            self._open_seg_ms = dur_ms if orms >= self.min_rms else 0
        else:
            self._open_seg_ms = 0
        if last_good is None:
            return None
        seg_start, seg_end = last_good
        # RMS gate: the door mic's ambient false-positives are quiet (RMS ~300-400);
        # real voice is loud (RMS 9000+). A segment below min_rms is a noise blip.
        seg_bytes = b''.join(fr for fr, _, _, _ in self.frames[seg_start:seg_end + 1])
        try:
            import numpy as _np
            a = _np.frombuffer(seg_bytes, dtype=_np.int16).astype(_np.float64)
            seg_rms = float(_np.sqrt(_np.mean(a ** 2))) if a.size else 0.0
        except Exception:
            seg_rms = 0.0
        if seg_rms < self.min_rms:
            return None
        # time since that segment's last frame (wall clock; a stall after the last
        # speech frame counts as silence toward the endpoint). 1 ms epsilon:
        # frame timestamps are float and the gate is a boundary comparison, so
        # an exact "silence_ms has elapsed" landing must not lose to float error.
        tail_ms = (now - self.frames[seg_end][3]) * 1000
        if tail_ms + 1.0 < self.silence_ms:
            return None
        # utterance = from just before the segment start through its end. Frames
        # inside the segment are contiguous speech (gap <= VOICE_GAP_MS), so
        # concatenating them is correct even across a sub-300ms drop.
        start = max(0, seg_start - 10)
        end = seg_end + 1
        utter = b''.join(fr for fr, _, _, _ in self.frames[start:end])
        self.frames = self.frames[end:]   # consume; keep post-end tail for next
        self._open_seg_ms = 0             # that segment just closed
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
        "\n\nLOCAL-PIPELINE NOTES: You are now spoken by a local text-to-speech engine, so "
        "keep replies short: one or two sentences, no lists, no markdown. Reply in the "
        "same language the visitor uses (English or Spanish). Tool calls add a short "
        "pause, so only call them when they genuinely help; when you do, call it once, "
        "then say one short line.")


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
        # range(0, len, chunk) — NOT len - len%chunk: the remainder (up to ~1 s)
        # must be queued too, or every reply loses its final word
        # (2026-09-17: 10-16% of each doorman reply was being dropped).
        for i in range(0, len(pcm), chunk):
            audio_q.put_nowait(pcm[i:i + chunk])
        await speaking.mark_active()
        # The local path has no receive-loop to clear the sticky active flag, so
        # schedule it at playback end: pcm is 24k s16le, paced in real time
        # downstream (aiortc emits one 20 ms frame per 20 ms of wall clock).
        play_s = len(pcm) / (24000 * 2)
        async def _release_echo_gate():
            try:
                await asyncio.sleep(play_s + 0.5)
                await speaking.end_speech()
                log.info("local: echo gate released after %.1fs playback", play_s)
            except Exception:
                pass
        asyncio.get_event_loop().create_task(_release_echo_gate())
        if activity:
            await activity.mark()
    log.info("tts: queued %d bytes for %d chars (engine=%s)",
             len(pcm), len(text), engine)
    return len(pcm)


def _claims_notification(text: str) -> bool:
    """True if the spoken text claims the homeowner was notified/alerted.

    The 27B frequently SAYS 'I've let the resident know' without actually calling
    notify_ryan (2026-09-18: 'I'll let the resident know' with no tool call; and a
    stale early notify not refreshed when the visitor adds carrier/signature
    details). This drives the code backstop: if the reply claims a notification but
    none fired this turn, fire a catch-up one. Match on an OWNER noun + ACTION verb
    (a bare 'I'll let you know' to the visitor is NOT a claim, so require an
    owner/resident/homeowner/owner noun)."""
    t = (text or '').lower()
    owner = any(w in t for w in
                ('resident', 'homeowner', 'home owner', 'owner',
                 'the house', 'the homeowner'))
    verb = any(w in t for w in
               ('know', 'notif', 'alert', 'told', 'tell', 'sent', 'gave'))
    return owner and verb


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
    notify_fired = False
    for _round in range(max_tool_rounds + 1):
        payload = {
            'model': cfg['DOORMAN_LLM_MODEL'],
            'messages': messages,
            'tools': openai_tools_schema(),
            'tool_choice': 'auto',
            'max_tokens': int(cfg.get('DOORMAN_LLM_MAX_TOKENS', 400)),
            'temperature': float(cfg.get('DOORMAN_LLM_TEMPERATURE', 0.8)),
            'top_p': 0.9,
            'stream': False,
            # 'options' (think/keep_alive) is Ollama-only; SGLang ignores unknown
            # top-level keys, so send it only for the Ollama fallback.
        }
        # qwen3 thinking chain: the Spark brain reasons BEFORE replying. With the
        # think chain on, a 300-token budget is eaten by reasoning -> empty spoken
        # reply (verified 2026-09-17: 13s, finish_reason=length, content=''). Off:
        # 1-5s, natural, tool-calling intact. SGLang exposes this via
        # chat_template_kwargs; the Ollama path uses options.think below.
        if cfg.get('DOORMAN_LLM_API_KEY') != 'ollama':
            payload['chat_template_kwargs'] = {
                'enable_thinking': bool(cfg.get('DOORMAN_LLM_THINK', False))}
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
            # Backstop: if the reply CLAIMS a notification but none fired this
            # turn, fire a catch-up notify_ryan so the owner actually hears from
            # the doorman even when the model only described the action verbally.
            if final and _claims_notification(final) and not notify_fired:
                try:
                    # Lead with the visitor's actual words - the phone truncates long
                    # notification bodies, so the content that matters (what the
                    # visitor said) must be at the START, not buried behind a
                    # "Doorman notified the homeowner..." prefix (title is already
                    # "Doorman", so the prefix was redundant and pushed the visitor
                    # text off the end where truncation cut it).
                    catch = ("Visitor at the door: \"%s\"" % user_text.strip()[:140])
                    log.info("notify backstop: reply claimed a notification "
                             "without a tool call -> firing catch-up notify_ryan")
                    ok, _val = await doorman_tools.notify_ryan(catch)
                    if activity:
                        await activity.mark()
                except Exception as e:
                    log.warning("notify backstop failed: %s", e)
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
                notify_fired = True
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
    # RING-SETTLE WAIT: never open the backchannel inside the AD410's ~7-8s
    # post-press "blue" window (verified 2026-09-25: opening bc=1 during that
    # window wedges the camera's RTSP server; after it is safe). A press tracked
    # within the window delays the open by the remainder; no press = open now.
    try:
        await ab.ring_settle_wait(cfg, log=log)
    except Exception as e:
        log.warning("ring-settle wait error: %s", str(e)[:80])
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
        min_speech_ms=int(cfg.get('DOORMAN_LOCAL_MIN_SPEECH_MS', 300)),
        aggressiveness=int(cfg.get('DOORMAN_LOCAL_VAD_AGGRESSIVENESS', 2)),
        max_ring_ms=int(cfg.get('DOORMAN_LOCAL_MAX_RING_MS', 10000)),
        min_rms=float(cfg.get('DOORMAN_LOCAL_MIN_RMS', 500)))
    # Gain multiplier on the doorbell mic RTSP. The raw doorbell signal is very
    # quiet (peak ~800/32768, RMS ~170). 40x pushed loud visitor speech to the
    # int16 ceiling (~peak 32767) so parakeet heard clipping -> 'silence/empty'.
    # 10x was tuned to quiet ambient, but a visitor talking CLOSE to the doorbell
    # still drives the substream to the int16 ceiling at 10x (verified 2026-09-18:
    # whole capture pinned at 32767, parakeet mangled 'I have a delivery' into
    # 'I haven't delivered'). The limiter caps the peak (~0.95) instead of
    # hard-clipping, while leaving quiet voices / ambient untouched.
    mic_gain = float(cfg.get('DOORMAN_LOCAL_MIC_GAIN', 10.0))
    mic_limiter = str(cfg.get('DOORMAN_LOCAL_MIC_LIMITER', 'true')).strip().lower() in ('1', 'true', 'yes', 'on')
    history = []

    async def mic_loop():
        """Same ffmpeg RTSP open + probe + retry as ab.mic_to_gemini, but frames feed
        the endpointer instead of a Gemini session."""
        mic_rtsp = cfg.get('DOORMAN_MIC_RTSP') or cfg.get('CAM_MIC_RTSP')
        if not mic_rtsp:
            log.warning("local mic: no DOORMAN_MIC_RTSP/CAM_MIC_RTSP configured; mic disabled")
            return
        t_open = time.monotonic()
        log.info("local mic: opening RTSP source %s (gain=%.0fx limiter=%s)",
                 (mic_rtsp or '').split('@')[-1], mic_gain, mic_limiter)
        proc = await ab.open_mic_ffmpeg(mic_rtsp, gain=mic_gain, limiter=mic_limiter)
        if proc is None:
            log.warning("local mic: RTSP audio failed to open after %.1fs", time.monotonic() - t_open)
            return
        log.info("local mic: RTSP audio open after %.1fs", time.monotonic() - t_open)
        _cap = None
        _cap_env = cfg.get('DOORMAN_LOCAL_DEBUG_CAPTURE')
        if _cap_env:
            import wave as _wave
            _cap = _wave.open(_cap_env, 'wb')
            _cap.setnchannels(1); _cap.setsampwidth(2); _cap.setframerate(SAMPLE_RATE)
            log.info("local mic: capturing to %s", _cap_env)
        acc = bytearray()
        _seen_data = 0
        _next_report = 50000          # log at >=50KB, then every 50KB
        _last_data_t = time.monotonic()
        _first_data_logged = False
        _speech_reported = -10.0
        # Stall recovery: during a two-way session the AD410 wedges under
        # concurrent-stream load and the go2rtc relay delivers audio in bursts with
        # 4-12s gaps (verified 2026-09-19: STALLED 4.7/7.2/12.0/6.4s in one
        # interaction). Those gaps swallowed whole utterances ("your name" was in a
        # 12s gap, so it never reached the endpointer). On a gap > the limit, kill
        # the ffmpeg pull and reopen it; the go2rtc source re-spins and the next
        # words come through. Bounded by _max_reopens so a dead source can't loop
        # forever. The endpointer's wall-clock gap detection (same commit) makes the
        # residual sub-limit gaps SPLIT an utterance instead of merging two.
        _stall_limit_s = float(cfg.get('DOORMAN_LOCAL_MIC_STALL_LIMIT_S', 5.0))
        _max_reopens = int(cfg.get('DOORMAN_LOCAL_MIC_MAX_REOPENS', 8))
        _reopens = 0
        try:
            while not stop_ev.is_set():
                if proc is None:
                    proc = await ab.open_mic_ffmpeg(mic_rtsp, gain=mic_gain, limiter=mic_limiter)
                    if proc is None:
                        log.warning("local mic: reopen probe failed (%d reopens)", _reopens + 1)
                        _reopens += 1
                        _last_data_t = time.monotonic()
                        if _reopens > _max_reopens:
                            log.warning("local mic: gave up after %d reopens", _reopens)
                            break
                        await asyncio.sleep(3.0)
                        continue
                    _last_data_t = time.monotonic()
                    log.info("local mic: RTSP audio (re)opened (reopen %d)", _reopens)
                    continue
                try:
                    data = await asyncio.wait_for(proc.stdout.read(3200), timeout=_stall_limit_s)
                except asyncio.TimeoutError:
                    log.warning("local mic: STALLED >%.1fs with no audio data -> reopening ffmpeg pull (reopen %d, total %d bytes)",
                                _stall_limit_s, _reopens + 1, _seen_data)
                    try: proc.kill()
                    except Exception: pass
                    proc = None
                    _reopens += 1
                    _last_data_t = time.monotonic()
                    if _reopens > _max_reopens:
                        log.warning("local mic: gave up after %d reopens", _reopens)
                        break
                    continue
                except Exception as e:
                    log.warning("local mic: read error %s -> reopening ffmpeg pull", str(e)[:80])
                    try: proc.kill()
                    except Exception: pass
                    proc = None
                    _reopens += 1
                    _last_data_t = time.monotonic()
                    continue
                if not data:
                    log.warning("local mic: ffmpeg EOF -> reopening ffmpeg pull (reopen %d, total %d bytes)", _reopens + 1, _seen_data)
                    try: proc.kill()
                    except Exception: pass
                    proc = None
                    _reopens += 1
                    _last_data_t = time.monotonic()
                    if _reopens > _max_reopens:
                        log.warning("local mic: gave up after %d reopens", _reopens)
                        break
                    continue
                if _cap:
                    try: _cap.writeframes(data)
                    except Exception: pass
                _now = time.monotonic()
                if _now - _last_data_t > 3.0:
                    log.warning("local mic: trickle - no audio data for %.1fs (total %d bytes)",
                                _now - _last_data_t, _seen_data)
                _last_data_t = _now
                if not _first_data_logged:
                    log.info("local mic: first audio data after %.1fs", _now - t_open)
                    _first_data_logged = True
                _seen_data += len(data)
                if _seen_data >= _next_report:
                    log.info("local mic: audio flowing (%d bytes total)", _seen_data)
                    _next_report += 50000
                # VAD-level visibility: has the endpointer seen any speech frames?
                sp_frames = ep.speech_frame_count()
                if sp_frames > 0 and _now - _speech_reported > 5.0:
                    log.info("local mic: VAD flagged speech in %d frames so far", sp_frames)
                    _speech_reported = _now
                acc += data
                while len(acc) >= VAD_FRAME_BYTES:
                    frame = bytes(acc[:VAD_FRAME_BYTES])
                    del acc[:VAD_FRAME_BYTES]
                    if await speaking.muted():
                        ep.push_frame(frame, muted=True)
                        continue
                    utter = ep.push_frame(frame, muted=False)
                    # VAD-aware idle: refresh the activity clock while the visitor is
                    # still mid-utterance (the endpointer has an open speech segment
                    # >= min_speech_ms). This keeps long CONNECTED speech alive so the
                    # idle watchdog cannot end the session mid-sentence. Without this,
                    # the idle clock only advanced on finalized STT text; a visitor
                    # talking in continuous phrases (never hitting the 700ms end-
                    # silence) went idle at 40s and got cut off mid-sentence (verified
                    # 2026-09-19: capture had loud continuous speech cap 26-68s but the
                    # endpointer never finalized it, so the session ended idle). The
                    # open segment only persists while VAD sees speech, so this does NOT
                    # re-open the old "ambient noise keeps the session alive" bug: quiet
                    # noise is an open segment of ~0ms (RMS-blip frames don't extend a
                    # qualifying segment), and once the visitor stops, open_segment_ms
                    # -> 0 and the clock starts counting again.
                    if ep.open_segment_ms() >= ep.min_speech_ms:
                        await activity.mark()
                    if utter:
                        await handle_utterance(utter)
        except asyncio.CancelledError:
            pass
        finally:
            if _cap:
                try: _cap.close()
                except Exception: pass
            try: proc.kill()
            except Exception: pass

    async def handle_utterance(pcm16: bytes):
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
        # Whisper anti-hallucination gate. Whisper (unlike Parakeet) will invent a
        # plausible short phrase on door ambient noise; the service returns a
        # per-utterance no_speech_prob (max over kept segments). Real door speech
        # measures ~0.00-0.05 here; noise hallucinations ~0.30+. Drop anything
        # above the threshold BEFORE it reaches the brain as a fake "visitor said",
        # and before activity.mark() so noise does not reset the idle clock.
        # (Parakeet has no such field -> None -> gate is a no-op, its empty result
        # already covers the common case.)
        nsp = stt.get('no_speech_prob')
        max_nsp = float(cfg.get('DOORMAN_LOCAL_STT_MAX_NSP', 0.25))
        if nsp is not None and nsp > max_nsp:
            log.info("local stt: dropped low-confidence speech '%s' "
                     "(no_speech_prob=%.3f > %.2f)", text, nsp, max_nsp)
            return
        # Real visitor audio only: mark activity AFTER the empty check, so ambient
        # noise that the endpointer flags but STT reads as silence does NOT reset
        # the idle clock. (2026-09-17: a person interaction rode its full 120 s
        # max because every 4-9 s of door noise kept marking activity, and the
        # next dog trigger was stuck behind it for 22 s.)
        await activity.mark()
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
    interaction_start = time.monotonic()
    # Grace window: don't count idle for the first few seconds so the cold
    # ffmpeg mic open + talkback settle + the visitor's first words don't race
    # the idle timer. The gemini path doesn't need this (it rides the already-
    # connected WebRTC track); the local path opens a separate RTSP stream.
    grace_s = min(float(cfg.get('DOORMAN_LOCAL_MIC_GRACE_S', 10.0)), duration_s - 5.0)
    try:
        try:
            if idle_timeout_s:
                while not stop_ev.is_set():
                    if time.monotonic() - interaction_start < grace_s:
                        await asyncio.sleep(0.5)
                        continue
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
    except asyncio.CancelledError:
        # The outer hard cap in doorman.py (INTERACTION_MAX_S + 15) cancels this
        # task BEFORE the watchdog's own duration. Without a finally, that
        # cancellation skips the talkback teardown below -> the WebRTC peer
        # connection + go2rtc signaling socket stay open, the mic task keeps
        # running, and go2rtc holds a consumer on the two-way relay that pins the
        # camera's bc=1 RTSP producer ("stuck in two-way mode"). Teardown in a
        # finally so it ALWAYS runs.
        log.info("local interaction cancelled by outer cap; tearing down talkback")
        raise
    finally:
        log.info("ending local interaction")
        mic_task.cancel()
        try:
            await asyncio.wait_for(asyncio.gather(mic_task, return_exceptions=True), timeout=2)
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
            await asyncio.wait_for(asyncio.gather(keep_task, return_exceptions=True), timeout=1)
        except Exception:
            pass
        try:
            await ws.close()
        except Exception:
            pass
    return 0
