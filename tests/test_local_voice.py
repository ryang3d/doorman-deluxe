#!/usr/bin/env python3
"""Tests for the local voice engine config + pure helpers (voice_local)."""
import os, sys
_here = os.path.dirname(os.path.abspath(__file__))
for _cand in (os.path.join(_here, '..', 'src'), '/app/src', os.path.join(_here, 'src')):
    if os.path.isdir(_cand):
        sys.path.insert(0, _cand)
        break
import doorman_config as dc

PASS, FAIL = [], []
def check(name, got, want):
    if got == want:
        PASS.append(name); print(f"  PASS {name}: {got!r}")
    else:
        FAIL.append(name); print(f"  FAIL {name}: got {got!r} want {want!r}")

SIL = b'\x00' * 960            # 30 ms of 16k s16le silence

def _speech_frames(n=6, off=0):
    """Return `n` consecutive 30 ms (480-sample) 16k s16le frames of REAL speech,
    resampled 24k->16k from the repo's out.wav fixture. Real speech flags as
    `is_speech` in webrtcvad reliably; a synthetic tone (even a 440 Hz sine) sits
    right at the VAD's frequency/energy boundary and only half-flags, which is why
    the original plan's square-wave _tone missed the endpoint (2026-09-16)."""
    import wave
    import numpy as np
    repo = os.path.join(_here, '..')
    path = os.path.join(repo, 'out.wav')
    with wave.open(path) as w:
        raw = w.readframes(w.getnframes())
    pcm24 = np.frombuffer(raw, dtype=np.int16).astype(np.float32)
    # resample 24k -> 16k, snapped to a multiple of 480 (one 30 ms frame)
    n16 = (len(pcm24) * 16000 // 24000) // 480 * 480
    idx = np.linspace(0, len(pcm24) - 1, n16)
    pcm16 = np.interp(idx, np.arange(len(pcm24)), pcm24).astype(np.int16)
    return [pcm16[(off + i) * 480:(off + i + 1) * 480].tobytes() for i in range(n)]

def test_endpointer():
    import voice_local as vl
    # Production-default pre-roll (300 ms) keeps the ring (keep_frames=24) large
    # enough to hold the full scenario; preres_ms=100 (the original plan) gave
    # keep=17 and truncated the voice frames before the end-silence scan ran.
    ep = vl.UtteranceEndpointer(silence_ms=300, min_speech_ms=150, preres_ms=300)
    out = None
    for _ in range(5):
        out = ep.push_frame(SIL, muted=False)      # idle silence
    assert out is None, out
    voice = _speech_frames(6)                       # 180 ms of real speech
    for f in voice:
        out = ep.push_frame(f, muted=False)
    assert out is None, "must not fire before end-silence"
    for _ in range(13):                             # trailing silence to >= 300 ms
        out = ep.push_frame(SIL, muted=False)
    assert out is not None and len(out) >= 480, "endpoint missed"
    # blip too short must not fire
    ep2 = vl.UtteranceEndpointer(silence_ms=300, min_speech_ms=300, preres_ms=300)
    out2 = None
    for _ in range(5): out2 = ep2.push_frame(SIL, muted=False)
    for f in voice[:4]: out2 = ep2.push_frame(f, muted=False)   # 120 ms < 300
    for _ in range(15): out2 = ep2.push_frame(SIL, muted=False)
    assert out2 is None, "short blip must not endpoint"
    # muted frames never start speech
    ep3 = vl.UtteranceEndpointer(silence_ms=300, min_speech_ms=150)
    for _ in range(5): ep3.push_frame(SIL, muted=False)
    for f in voice: ep3.push_frame(f, muted=True)
    for _ in range(15): ep3.push_frame(SIL, muted=False)
    assert ep3._check_endpoint() is None, "muted voice must not count"
    check("endpointer", "ok", "ok")

def test_voice_map():
    import voice_local as vl
    cfg = {'DOORMAN_TTS_PROFILE_EN': 'ENPID', 'DOORMAN_TTS_PROFILE_ES': 'ESPID',
           'DOORMAN_TTS_ENGINE_EN': 'chatterbox_turbo', 'DOORMAN_TTS_ENGINE_ES': 'chatterbox'}
    check("lang es -> es engine", vl.tts_profile_for_language('es', cfg), ('ESPID', 'chatterbox'))
    check("lang es-ES -> es engine", vl.tts_profile_for_language('es-ES', cfg), ('ESPID', 'chatterbox'))
    check("lang en -> en engine", vl.tts_profile_for_language('en', cfg), ('ENPID', 'chatterbox_turbo'))
    check("lang '' -> en engine", vl.tts_profile_for_language('', cfg), ('ENPID', 'chatterbox_turbo'))

def test_tool_schema():
    import voice_local as vl
    s = vl.openai_tools_schema()
    names = sorted(t['function']['name'] for t in s)
    check("tools", names, ['notify_ryan', 'snapshot_front_door'])

# ---------------------------------------------------------------- HTTP-level stub tests
import json, threading, http.server, asyncio

class _Stub(http.server.BaseHTTPRequestHandler):
    kind = 'generic'
    def log_message(self, *a): pass
    def do_POST(self):
        length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(length)
        if self.kind == 'llm':
            # first call: request a tool; second call: final answer
            if not hasattr(_Stub, '_n'): _Stub._n = 0
            _Stub._n += 1
            if _Stub._n == 1:
                msg = {'role': 'assistant', 'content': None,
                       'tool_calls': [{'id': 'c1', 'type': 'function',
                                        'function': {'name': 'notify_ryan',
                                                     'arguments': '{"message": "x"}'}}]}
            else:
                msg = {'role': 'assistant', 'content': 'Package noted.'}
            out = json.dumps({'choices': [{'message': msg}]}).encode()
            self.send_response(200); self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(out))); self.end_headers()
            self.wfile.write(out)
        elif self.kind == 'stt':
            out = json.dumps({'text': 'hi', 'language': 'es', 'seconds': 0.1}).encode()
            self.send_response(200); self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(out))); self.end_headers()
            self.wfile.write(out)
        elif self.kind == 'tts':
            # voicebox /generate: async queue; returns a generation record
            out = json.dumps({'id': 'stubgen1', 'status': 'generating',
                              'text': json.loads(body).get('text', '')}).encode()
            self.send_response(200); self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(out))); self.end_headers()
            self.wfile.write(out)
    def do_GET(self):
        if self.kind == 'tts':
            # /generate/<id>/status: SSE with a completed record
            out = ('data: {"id": "stubgen1", "status": "completed", '
                   '"duration": 1.0, "error": null}').encode()
            # /audio/<id>: 1s of 24k s16le wav
            import wave as _w, io as _io
            buf = _io.BytesIO()
            with _w.open(buf, 'wb') as w:
                w.setnchannels(1); w.setsampwidth(2); w.setframerate(24000)
                w.writeframes(b'\x00\x01' * 24000)   # 1 s
            audio = buf.getvalue()
            if self.path.startswith('/generate/'):
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.send_header('Content-Length', str(len(out))); self.end_headers()
                self.wfile.write(out)
            elif self.path.startswith('/audio/'):
                self.send_response(200); self.send_header('Content-Type', 'audio/wav')
                self.send_header('Content-Length', str(len(audio))); self.end_headers()
                self.wfile.write(audio)
            else:
                self.send_response(404); self.end_headers()
        else:
            self.send_response(404); self.end_headers()

def _start_stub(kind, port):
    h = type('H', (_Stub,), {'kind': kind})
    srv = http.server.HTTPServer(('127.0.0.1', port), h)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv

def test_clients():
    import voice_local as vl
    import doorman_tools
    async def fake_notify(message, cfg=None, image_path=None):
        return True, 'notification sent'
    _orig = doorman_tools.notify_ryan
    doorman_tools.notify_ryan = fake_notify
    cfg = {'DOORMAN_STT_BASE_URL': 'http://127.0.0.1:18301',
           'DOORMAN_TTS_BASE_URL': 'http://127.0.0.1:18880',
           'DOORMAN_LLM_BASE_URL': 'http://127.0.0.1:18134',
           'DOORMAN_LLM_MODEL': 'stub', 'DOORMAN_LLM_KEEP_ALIVE': '5m',
           'DOORMAN_TTS_PROFILE_EN': 'enpid', 'DOORMAN_TTS_PROFILE_ES': 'espid',
           'DOORMAN_TTS_ENGINE_EN': 'chatterbox_turbo', 'DOORMAN_TTS_ENGINE_ES': 'chatterbox'}
    srvs = [_start_stub('stt', 18301), _start_stub('tts', 18880), _start_stub('llm', 18134)]
    try:
        async def main():
            stt = await vl.transcribe_utterance(b'\x00\x00' * 480, cfg)
            check("stt text", stt['text'], 'hi')
            check("stt lang", stt['language'], 'es')
            q = asyncio.Queue()
            class FakeSpeaking:
                _lang = ''
                async def mark_active(self): pass
            sp = FakeSpeaking()
            sp._lang = stt['language']           # es -> multilingual engine
            n = await vl.synthesize('hola', cfg, q, sp)
            check("tts queued bytes", n > 0, True)
            _Stub._n = 0
            txt, hist = await vl.brain_turn('sys', [], 'there is a package', cfg)
            check("brain final", txt, 'Package noted.')
            check("brain hist has tool result",
                  any(m.get('role') == 'tool' for m in hist), True)
        asyncio.run(main())
    finally:
        doorman_tools.notify_ryan = _orig
        for s in srvs: s.shutdown()

def main():
    cfg = dc.load()
    check("engine default gemini", cfg.get('DOORMAN_VOICE_ENGINE'), 'gemini')
    check("llm model default", cfg.get('DOORMAN_LLM_MODEL'), 'qwen3.8-27b')
    check("llm base url default", cfg.get('DOORMAN_LLM_BASE_URL'),
          'http://<llm-host>:30000/v1')
    check("tts url default", cfg.get('DOORMAN_TTS_BASE_URL'), 'http://127.0.0.1:17600')
    check("tts engine en", cfg.get('DOORMAN_TTS_ENGINE_EN'), 'chatterbox_turbo')
    check("stt url default", cfg.get('DOORMAN_STT_BASE_URL'), 'http://127.0.0.1:10301')
    check("keep-warm settle default", cfg.get('DOORMAN_KEEP_WARM_SETTLE_S'), 30)
    check("keep-warm interval default", cfg.get('DOORMAN_KEEP_WARM_INTERVAL_S'), 1500)
    check("local lang fallback default", cfg.get('DOORMAN_LOCAL_LANG_FALLBACK'), 'auto')
    test_endpointer()
    test_voice_map()
    test_tool_schema()
    test_clients()
    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        sys.exit(1)

if __name__ == '__main__':
    main()
