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


def test_animal_lines_pool():
    assert 'cat' in dp.ANIMAL_LINES and len(dp.ANIMAL_LINES['cat']) >= 3
    assert 'dog' in dp.ANIMAL_LINES and len(dp.ANIMAL_LINES['dog']) >= 3
    for line in dp.ANIMAL_LINES['cat'] + dp.ANIMAL_LINES['dog']:
        assert isinstance(line, str) and line.strip()
    print("PASS animal lines pool has cat + dog entries")


def test_animal_greeting_line():
    g = dp.animal_greeting_line('cat')
    assert g in dp.ANIMAL_LINES['cat'], g
    g2 = dp.animal_greeting_line('dog')
    assert g2 in dp.ANIMAL_LINES['dog'], g2
    g3 = dp.animal_greeting_line('bird')
    assert isinstance(g3, str) and g3.strip()  # unknown label -> generic line
    print("PASS animal_greeting_line returns a pool line (generic fallback for others)")


def test_animal_prompt():
    p = dp.build_doorman_prompt(animal_label='cat')
    assert 'cat' in p
    assert 'Do not call any tools' in p
    assert 'Say the greeting below' in p  # the fun-instruction (picks a concrete line)
    # a concrete line from the cat pool must be embedded
    assert any(line in p for line in dp.ANIMAL_LINES['cat'])
    # must not look like the human unknown-visitor prompt
    assert 'NOT recognized as a household member' not in p
    # person prompt must be unchanged
    hp = dp.build_doorman_prompt(recognized_name=None)
    assert 'NOT recognized as a household member' in hp
    print("PASS animal prompt is animal-aware + playful; person prompt unchanged")


def test_animal_trigger_text():
    t = dp.interaction_trigger_text(label='dog', animal=True)
    assert 'dog' in t and 'one-liner' in t
    t2 = dp.interaction_trigger_text(label='person')
    assert 'detected at the door' in t2 and 'one-liner' not in t2
    print("PASS animal trigger text; person trigger text unchanged")


if __name__ == '__main__':
    test_config_default_voice()
    test_config_env_override()
    test_animal_max_s_numeric()
    test_animal_labels()
    test_decide_action_matrix()
    test_animal_lines_pool()
    test_animal_greeting_line()
    test_animal_prompt()
    test_animal_trigger_text()
    print("ALL ANIMAL TRIGGER TESTS PASS")
