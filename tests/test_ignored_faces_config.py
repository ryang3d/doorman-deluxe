#!/usr/bin/env python3
"""Config tests for DOORMAN_IGNORED_FACES.

Run: .venv/bin/python tests/test_ignored_faces_config.py   (from ~/doorman)
"""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
import doorman_config as dc


def _clear():
    for k in list(os.environ.keys()):
        if k.startswith('DOORMAN_'):
            del os.environ[k]


def test_default_empty_set():
    _clear()
    assert dc.load()['DOORMAN_IGNORED_FACES'] == set(), dc.load()['DOORMAN_IGNORED_FACES']
    print("PASS default empty ignore set")


def test_parse_comma_separated_case_insensitive():
    _clear()
    os.environ['DOORMAN_IGNORED_FACES'] = ' Bob ,Alice,bob,  dave '
    s = dc.load()['DOORMAN_IGNORED_FACES']
    assert s == {'bob', 'alice', 'dave'}, s   # deduped + lower-cased
    print("PASS comma-split + lower + dedupe")


def test_empty_string_disables():
    _clear()
    os.environ['DOORMAN_IGNORED_FACES'] = ''
    assert dc.load()['DOORMAN_IGNORED_FACES'] == set()
    print("PASS empty string -> empty set")


if __name__ == '__main__':
    test_default_empty_set()
    test_parse_comma_separated_case_insensitive()
    test_empty_string_disables()
    print("ALL IGNORED-FACES CONFIG TESTS PASS")
