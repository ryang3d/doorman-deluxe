#!/usr/bin/env python3
"""Unit tests for the door-zone occupancy gate (DOORMAN_PERSON_GATE /
DOORMAN_PERSON_HOLD_S). Gate = supplemental HA occupancy sensor that must be
held for PERSON_HOLD_S before a Frigate person/face trigger may fire.

Run (inside the doorman container):
  docker cp tests doorman:/tmp/tests
  docker exec doorman sh -c 'cd /tmp/tests && export PYTHONPATH=/app/src && python test_doorzone_gate.py'
"""
import asyncio, os, sys, time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
import doorman as dm
import doorman_config as dc


def _clear():
    for k in list(os.environ):
        if k.startswith('DOORMAN_'):
            del os.environ[k]


# ---------------------------------------------------------------- config layer
def test_gate_default_entity():
    _clear()
    assert dc.load()['DOORMAN_PERSON_GATE'] == 'binary_sensor.front_patio_motion_zone_person_occupancy'
    print("PASS gate default = front patio zone occupancy sensor")


def test_hold_default_and_override():
    _clear()
    assert dc.load()['DOORMAN_PERSON_HOLD_S'] == 5.0
    os.environ['DOORMAN_PERSON_HOLD_S'] = '8'
    assert dc.load()['DOORMAN_PERSON_HOLD_S'] == 8.0
    print("PASS hold default 5.0 + env override numeric")


def test_gate_empty_disables():
    _clear()
    os.environ['DOORMAN_PERSON_GATE'] = ''
    assert dc.load()['DOORMAN_PERSON_GATE'] == ''
    print("PASS empty DOORMAN_PERSON_GATE disables the gate")


if __name__ == '__main__':
    test_gate_default_entity()
    test_hold_default_and_override()
    test_gate_empty_disables()
    print("ALL DOORZONE GATE TESTS PASS")
