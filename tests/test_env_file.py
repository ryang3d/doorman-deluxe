#!/usr/bin/env python3
"""Tests for doorman_config.load_env_file(): .env -> os.environ (file wins).

Run: .venv/bin/python -m pytest tests/test_env_file.py -q
"""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
import doorman_config as dc


def test_load_env_file_sets_os_environ(tmp_path):
    f = tmp_path / '.env'
    f.write_text('# comment\nDOORMAN_UI_PORT=9999\nDOORMAN_UI_ENABLED=false\n\n')
    os.environ.pop('DOORMAN_UI_PORT', None)
    os.environ.pop('DOORMAN_UI_ENABLED', None)
    try:
        dc.load_env_file(str(f))
        assert os.environ['DOORMAN_UI_PORT'] == '9999'
        assert os.environ['DOORMAN_UI_ENABLED'] == 'false'
    finally:
        os.environ.pop('DOORMAN_UI_PORT', None)
        os.environ.pop('DOORMAN_UI_ENABLED', None)


def test_load_env_file_overrides_existing_env(tmp_path):
    f = tmp_path / '.env'
    f.write_text('DOORMAN_UI_PORT=7777\n')
    os.environ['DOORMAN_UI_PORT'] = '8090'
    try:
        dc.load_env_file(str(f))
        # file wins over existing env
        assert os.environ['DOORMAN_UI_PORT'] == '7777'
    finally:
        os.environ.pop('DOORMAN_UI_PORT', None)


def test_load_env_file_missing_is_noop(tmp_path):
    # must not raise; returns empty dict
    out = dc.load_env_file(str(tmp_path / 'nope.env'))
    assert out == {}


def test_defaults_expose_ui_keys():
    for k in ('DOORMAN_UI_PORT', 'DOORMAN_UI_ENABLED', 'DOORMAN_DATA_DIR'):
        os.environ.pop(k, None)
    cfg = dc.load()
    assert cfg['DOORMAN_DATA_DIR'] == '/data'
    assert cfg['DOORMAN_UI_PORT'] == 8090
    assert isinstance(cfg['DOORMAN_UI_PORT'], int)
    assert cfg['DOORMAN_UI_ENABLED'] is True
