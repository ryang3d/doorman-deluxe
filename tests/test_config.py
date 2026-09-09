#!/usr/bin/env python3
"""Unit tests for doorman_config.load(): env override, file fallback, defaults.

Run: .venv/bin/python tests/test_config.py   (from ~/doorman)
"""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
import doorman_config as dc


def _clear_doorman_env():
    for k in list(os.environ.keys()):
        if k.startswith('DOORMAN_'):
            del os.environ[k]


def test_defaults_when_unset():
    _clear_doorman_env()
    cfg = dc.load()
    assert cfg['MQTT_HOST'] == '<ha-host>', cfg['MQTT_HOST']
    assert cfg['FRIGATE_URL'] == 'http://<frigate-host>:5001', cfg['FRIGATE_URL']
    assert cfg['FRONT_CAMERA'] == 'front_doorbell'
    assert cfg['MQTT_PORT'] == 1883 and isinstance(cfg['MQTT_PORT'], int)
    # these are only in DEFAULTS (not in any profile file), so they prove the
    # hardcoded default is used when nothing overrides it
    assert cfg['INTERACTION_COOLDOWN_S'] == 20.0, cfg['INTERACTION_COOLDOWN_S']
    assert cfg['DOORMAN_SNAPSHOT_HTTP_PORT'] == 8120, cfg['DOORMAN_SNAPSHOT_HTTP_PORT']
    assert cfg['DOORMAN_SNAPSHOT_HTTP_ADVERTISE_HOST'] == '<doorman-host>'
    print("PASS defaults fallback")


def test_env_override():
    _clear_doorman_env()
    os.environ['DOORMAN_MQTT_HOST'] = '10.0.0.5'
    os.environ['DOORMAN_MQTT_PORT'] = '1884'
    os.environ['DOORMAN_PERSONALIZED_GREETING'] = 'false'
    os.environ['DOORMAN_SNAPSHOT_HTTP_PORT'] = '9999'
    cfg = dc.load()
    assert cfg['MQTT_HOST'] == '10.0.0.5', cfg['MQTT_HOST']
    assert cfg['MQTT_PORT'] == 1884 and isinstance(cfg['MQTT_PORT'], int)
    assert cfg['DOORMAN_PERSONALIZED_GREETING'] is False
    assert cfg['DOORMAN_SNAPSHOT_HTTP_PORT'] == 9999 and isinstance(cfg['DOORMAN_SNAPSHOT_HTTP_PORT'], int)
    print("PASS env override")


def test_env_empty_string_overrides():
    _clear_doorman_env()
    os.environ['DOORMAN_GEMINI_API_KEY'] = ''
    cfg = dc.load()
    assert cfg['GEMINI_API_KEY'] == '', repr(cfg['GEMINI_API_KEY'])
    print("PASS empty env overrides file/default")


def test_profile_files_loaded():
    _clear_doorman_env()
    if os.path.exists(dc.PROFILE_ENV):
        cfg = dc.load()
        assert cfg['HASS_TOKEN'], "expected HASS_TOKEN from profile .env"
        print("PASS profile .env fallback (HASS_TOKEN present)")
    else:
        print("SKIP profile .env fallback (file absent on this host)")


if __name__ == '__main__':
    test_defaults_when_unset()
    test_env_override()
    test_env_empty_string_overrides()
    test_profile_files_loaded()
    print("ALL CONFIG TESTS PASS")
