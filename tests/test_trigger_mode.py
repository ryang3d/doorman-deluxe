#!/usr/bin/env python3
"""Unit tests for DOORMAN_TRIGGER_MODE / DOORMAN_DOORBELL_SENSOR config parsing.

Run: .venv/bin/python tests/test_trigger_mode.py   (from ~/doorman)
"""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
import doorman_config as dc


def _clear_doorman_env():
    for k in list(os.environ.keys()):
        if k.startswith('DOORMAN_'):
            del os.environ[k]


def test_default_person_mode():
    _clear_doorman_env()
    cfg = dc.load()
    assert cfg['DOORMAN_TRIGGER_MODE'] == 'person', cfg['DOORMAN_TRIGGER_MODE']
    assert cfg['DOORMAN_DOORBELL_SENSOR'] == 'binary_sensor.doorbell_pressed'
    print("PASS default = person mode, default sensor")


def test_env_doorbell_mode():
    _clear_doorman_env()
    os.environ['DOORMAN_TRIGGER_MODE'] = 'doorbell'
    os.environ['DOORMAN_DOORBELL_SENSOR'] = 'binary_sensor.front_door_doorbell'
    cfg = dc.load()
    assert cfg['DOORMAN_TRIGGER_MODE'] == 'doorbell'
    assert cfg['DOORMAN_DOORBELL_SENSOR'] == 'binary_sensor.front_door_doorbell'
    print("PASS env override to doorbell mode + custom sensor")


def test_mode_normalized_lower():
    _clear_doorman_env()
    os.environ['DOORMAN_TRIGGER_MODE'] = 'DOORBELL'
    cfg = dc.load()
    # the orchestrator lowercases it at dispatch time; config just carries the value
    assert cfg['DOORMAN_TRIGGER_MODE'] == 'DOORBELL'
    assert cfg['DOORMAN_TRIGGER_MODE'].lower() == 'doorbell'
    print("PASS mode value carried (orchestrator lowercases)")


if __name__ == '__main__':
    test_default_person_mode()
    test_env_doorbell_mode()
    test_mode_normalized_lower()
    print("ALL TRIGGER MODE TESTS PASS")
