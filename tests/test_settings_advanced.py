"""Schema 'advanced' flag: rarely-touched fields carry spec['advanced'] so the
UI can hide them behind a per-section 'Show advanced' toggle.

The flag lives in field_map() output (not the FIELDS tuple) so tuple arity
stays 9 and existing positional access is untouched.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
import ui_schema as s  # noqa: E402


def test_every_spec_has_advanced_flag():
    for key, spec in s.field_map().items():
        assert 'advanced' in spec, key
        assert isinstance(spec['advanced'], bool), key


def test_tuning_fields_are_advanced():
    fm = s.field_map()
    advanced = {k for k, v in fm.items() if v['advanced']}
    for k in ['DOORMAN_LLM_TEMPERATURE', 'DOORMAN_LLM_MAX_TOKENS', 'DOORMAN_LLM_THINK',
              'DOORMAN_LOCAL_SILENCE_MS', 'DOORMAN_LOCAL_MIN_SPEECH_MS',
              'DOORMAN_LOCAL_MIN_RMS', 'DOORMAN_LOCAL_MAX_RING_MS',
              'DOORMAN_LOCAL_VAD_AGGRESSIVENESS', 'DOORMAN_LOCAL_MIC_GAIN',
              'DOORMAN_LOCAL_MIC_LIMITER', 'DOORMAN_LOCAL_STT_MAX_NSP',
              'DOORMAN_KEEP_WARM_SETTLE_S', 'DOORMAN_KEEP_WARM_INTERVAL_S']:
        assert k in advanced, f"{k} should be advanced"


def test_core_fields_are_not_advanced():
    fm = s.field_map()
    for k in ['HASS_URL', 'DOORMAN_VOICE_ENGINE', 'DOORMAN_LLM_BASE_URL', 'MQTT_HOST']:
        assert k in fm, f"missing {k}"
        assert not fm[k]['advanced'], f"{k} should stay visible"
