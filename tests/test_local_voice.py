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
    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        sys.exit(1)

if __name__ == '__main__':
    main()
