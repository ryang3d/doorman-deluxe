#!/usr/bin/env python3
"""Tests for the Doorman UI API (ui_api.py).

Run: .venv/bin/python -m pytest tests/test_ui_api.py -q
"""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
import pytest
import doorman_config as dc
import ui_api


@pytest.fixture
def client(aiohttp_client, tmp_path, monkeypatch):
    monkeypatch.setenv('DOORMAN_DATA_DIR', str(tmp_path))
    monkeypatch.setattr(dc, 'ENV_FILE_PATH', str(tmp_path / '.env'))
    app = ui_api.build_app()
    return aiohttp_client(app)


@pytest.mark.asyncio
async def test_config_get_returns_fields(client):
    async with await client as c:
        r = await c.get('/api/config')
        assert r.status == 200
        d = await r.json()
        keys = {f['key'] for f in d['fields']}
        assert 'DOORMAN_TRIGGER_MODE' in keys
        assert 'DOORMAN_VOICE_ENGINE' in keys
        assert 'Home Assistant' in d['groups']
        # restart_required flag present on every field
        for f in d['fields']:
            assert 'restart_required' in f


@pytest.mark.asyncio
async def test_config_post_writes_env_and_flags_restart(client, tmp_path):
    async with await client as c:
        r = await c.post('/api/config', json={'values': {
            'DOORMAN_TRIGGER_MODE': 'doorbell',   # restart_required
            'DOORMAN_ANIMAL_MAX_S': '60',         # restart_required
            'DOORMAN_LLM_MODEL': 'test-model'}})  # hot
        assert r.status == 200
        d = await r.json()
        assert d['needs_restart'] is True
        assert 'DOORMAN_TRIGGER_MODE' in d['restart_required']
        assert 'DOORMAN_LLM_MODEL' in d['applied']
        env = open(dc.ENV_FILE_PATH).read()
        assert 'DOORMAN_TRIGGER_MODE=doorbell' in env
        assert 'DOORMAN_LLM_MODEL=test-model' in env


@pytest.mark.asyncio
async def test_config_post_preserves_unrelated_lines(client, tmp_path):
    envf = dc.ENV_FILE_PATH
    open(envf, 'w').write('# keep me\nDOORMAN_MQTT_HOST=1.2.3.4\n')
    async with await client as c:
        r = await c.post('/api/config', json={'values': {
            'DOORMAN_ANIMAL_MAX_S': '99'}})
        assert r.status == 200
        env = open(envf).read()
        assert '# keep me' in env
        assert 'DOORMAN_MQTT_HOST=1.2.3.4' in env
        assert 'DOORMAN_ANIMAL_MAX_S=99' in env


@pytest.mark.asyncio
async def test_config_post_unknown_keys_ignored(client):
    async with await client as c:
        r = await c.post('/api/config', json={'values': {
            'DOORMAN_ANIMAL_MAX_S': '60', 'NOT_A_KEY': 'x'}})
        assert r.status == 200
        env = open(dc.ENV_FILE_PATH).read()
        assert 'NOT_A_KEY' not in env


@pytest.mark.asyncio
async def test_status_shape(client):
    async with await client as c:
        r = await c.get('/api/status')
        assert r.status == 200
        d = await r.json()
        for k in ('engine', 'trigger_mode', 'camera_healthy',
                  'in_conversation', 'last_visit', 'uptime_s', 'version'):
            assert k in d, 'missing %s' % k
        assert d['in_conversation'] is False


@pytest.mark.asyncio
async def test_history_and_session_roundtrip(client):
    import transcripts as t
    t.begin_session(trigger='doorbell', name='Ryan')
    t.msg('visitor', 'Hi')
    t.end_session()
    async with await client as c:
        r = await c.get('/api/history')
        assert r.status == 200
        rows = await r.json()
        assert rows and rows[0]['recognized_name'] == 'Ryan'
        sid = rows[0]['session_id']
        r2 = await c.get('/api/sessions/' + sid)
        assert r2.status == 200
        s = await r2.json()
        assert len(s['messages']) == 1
        r3 = await c.get('/api/sessions/nope')
        assert r3.status == 404


@pytest.mark.asyncio
async def test_root_serves_index_not_dir_listing(client):
    async with await client as c:
        r = await c.get('/')
        assert r.status == 200
        body = await r.text()
        assert 'Index of' not in body
        assert 'Doorman' in body


@pytest.mark.asyncio
async def test_restart_endpoint(client):
    async with await client as c:
        r = await c.get('/api/restart')
        assert r.status == 405   # POST only
    # (POSTing for real would os.execv the test process, so GET/405 is the safe assert)
