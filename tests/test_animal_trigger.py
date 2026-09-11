#!/usr/bin/env python3
"""Unit tests for cat/dog recognition handling in Doorman's Frigate trigger.

Run: .venv/bin/python tests/test_animal_trigger.py   (from ~/doorman)
"""
import asyncio, os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
import doorman as dm
import doorman_config as dc
import doorman_prompt as dp
import doorman_tools as tools


def _clear():
    for k in list(os.environ):
        if k.startswith('DOORMAN_'):
            del os.environ[k]


def test_config_default_voice():
    _clear()
    assert dc.load()['DOORMAN_ANIMAL_BEHAVIOR'] == 'voice'
    print("PASS default behavior = voice")


def test_config_env_override():
    _clear()
    os.environ['DOORMAN_ANIMAL_BEHAVIOR'] = 'notify'
    assert dc.load()['DOORMAN_ANIMAL_BEHAVIOR'] == 'notify'
    print("PASS env override to notify")


def test_animal_max_s_numeric():
    _clear()
    assert dc.load()['DOORMAN_ANIMAL_MAX_S'] == 45.0
    os.environ['DOORMAN_ANIMAL_MAX_S'] = '30'
    assert dc.load()['DOORMAN_ANIMAL_MAX_S'] == 30.0
    print("PASS ANIMAL_MAX_S numeric normalize + override")


def test_animal_labels():
    assert 'cat' in dm.ANIMAL_LABELS and 'dog' in dm.ANIMAL_LABELS
    assert 'person' not in dm.ANIMAL_LABELS
    assert 'bird' not in dm.ANIMAL_LABELS
    print("PASS animal labels (cat/dog only, no bird)")


def test_decide_action_matrix():
    assert dm._decide_trigger_action('cat', 'voice') == 'animal-voice'
    assert dm._decide_trigger_action('dog', 'notify') == 'animal-notify'
    assert dm._decide_trigger_action('cat', 'off') == 'animal-off'
    assert dm._decide_trigger_action('cat', '') == 'animal-voice'    # default
    assert dm._decide_trigger_action('cat', None) == 'animal-voice'  # default
    assert dm._decide_trigger_action('person', 'voice') == 'person'
    assert dm._decide_trigger_action('bird', 'voice') == 'person'
    print("PASS decide_action matrix")


if __name__ == '__main__':
    test_config_default_voice()
    test_config_env_override()
    test_animal_max_s_numeric()
    test_animal_labels()
    test_decide_action_matrix()
    print("ALL ANIMAL TRIGGER TESTS PASS")
