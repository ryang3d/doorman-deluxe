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


@pytest.mark.asyncio
async def test_notify_test_endpoint_sends_via_notify_ryan(client, monkeypatch):
    import doorman_tools
    sent = {}
    async def fake_notify(message, cfg=None, image_path=None):
        sent['message'] = message
        sent['cfg'] = cfg
        return True, 'notification sent'
    monkeypatch.setattr(doorman_tools, 'notify_ryan', fake_notify)
    async with await client as c:
        r = await c.post('/api/notify-test', json={})
        assert r.status == 200
        d = await r.json()
        assert d['ok'] is True
        assert 'test notification' in sent['message'].lower()
        # it called notify_ryan with the creds dict, not None
        assert sent['cfg'] is not None and 'frigate_url' in sent['cfg']


@pytest.mark.asyncio
async def test_notify_test_endpoint_reports_failure(client, monkeypatch):
    import doorman_tools
    async def fake_notify_fail(message, cfg=None, image_path=None):
        return False, 'notify failed: 500'
    monkeypatch.setattr(doorman_tools, 'notify_ryan', fake_notify_fail)
    async with await client as c:
        r = await c.post('/api/notify-test', json={})
        assert r.status == 200
        d = await r.json()
        assert d['ok'] is False
        assert '500' in d['detail']


@pytest.mark.asyncio
async def test_prompts_get_returns_seven_and_defaults(client):
    async with await client as c:
        r = await c.get('/api/prompts')
        assert r.status == 200
        d = await r.json()
        assert set(d['prompts'].keys()) == {'system', 'animal', 'animal_cat_lines',
                                            'animal_dog_lines', 'animal_generic_line',
                                            'trigger', 'local_notes'}
        assert 'system' in d['order']
        # each entry exposes text + default + placeholders
        e = d['prompts']['system']
        for k in ('text', 'default', 'placeholders'):
            assert k in e
        assert e['text'] == e['default']   # nothing overridden yet


@pytest.mark.asyncio
async def test_prompts_post_saves_and_get_reflects(client):
    async with await client as c:
        r = await c.post('/api/prompts', json={'values': {'system': 'DRAFT {identity}'}})
        assert r.status == 200
        assert (await r.json())['saved'] == ['system']
        r2 = await c.get('/api/prompts')
        assert (await r2.json())['prompts']['system']['text'] == 'DRAFT {identity}'


@pytest.mark.asyncio
async def test_prompts_reset(client):
    async with await client as c:
        await c.post('/api/prompts', json={'values': {'trigger': 'X {facts}'}})
        r = await c.post('/api/prompts/reset', json={'key': 'trigger'})
        assert r.status == 200 and (await r.json())['ok'] is True
        r2 = await c.get('/api/prompts')
        assert (await r2.json())['prompts']['trigger']['text'] == (await r2.json())['prompts']['trigger']['default']


@pytest.mark.asyncio
async def test_prompts_preview_renders_recognized_and_unknown(client):
    async with await client as c:
        r = await c.post('/api/prompts/preview',
                         json={'system': 'Hi {identity} for {household_hint}'})
        assert r.status == 200
        d = await r.json()
        assert 'Ryan' in d['system']['recognized']
        assert 'NOT recognized' in d['system']['unknown']


@pytest.mark.asyncio
async def test_prompts_malformed_bodies_dont_500(client):
    async with await client as c:
        # valid JSON but not an object
        r = await c.post('/api/prompts', json=[1, 2, 3])
        assert r.status == 400
        # values is a list, not an object
        r = await c.post('/api/prompts', json={'values': ['x']})
        assert r.status == 400
        # reset with a non-object body and a non-string key
        r = await c.post('/api/prompts/reset', json='str')
        assert r.status == 200 and (await r.json())['ok'] is False
        r = await c.post('/api/prompts/reset', json={'key': 5})
        assert r.status == 200 and (await r.json())['ok'] is False
        # preview with a non-string draft falls back to the saved prompt
        r = await c.post('/api/prompts/preview', json={'system': 42})
        assert r.status == 200
        d = await r.json()
        assert 'recognized' in d['system']
        # bad json to the write endpoint is a 400, not a 500
        r = await c.post('/api/prompts', data='{not json',
                         headers={'Content-Type': 'application/json'})
        assert r.status == 400
