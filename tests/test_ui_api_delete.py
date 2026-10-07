import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
import pytest
import doorman_config as dc
import ui_api
import transcripts as tr


@pytest.fixture(autouse=True)
def _clear_state():
    tr._CURRENT = None
    tr._START_MONO = None
    yield
    tr._CURRENT = None
    tr._START_MONO = None


@pytest.fixture
def client(aiohttp_client, tmp_path, monkeypatch):
    monkeypatch.setenv('DOORMAN_DATA_DIR', str(tmp_path))
    monkeypatch.setattr(dc, 'ENV_FILE_PATH', str(tmp_path / '.env'))
    app = ui_api.build_app()
    return aiohttp_client(app)


def _finished_session(tmp_path, snapshot=True):
    s = tr.begin_session(trigger='doorbell')
    if snapshot:
        snap = tmp_path / (s['session_id'] + '.jpg')
        snap.write_bytes(b'\xff\xd8\xff\xe0')
        tr.current()['snapshot'] = str(snap)
    tr.msg('visitor', 'hello')
    return tr.end_session()['session_id']


@pytest.mark.asyncio
async def test_delete_unknown_session_404(client):
    async with await client as c:
        r = await c.delete('/api/sessions/no-such-session')
        assert r.status == 404
        assert (await r.json())['error'] == 'not found'


@pytest.mark.asyncio
async def test_delete_session_removes_row_and_snapshot(client, tmp_path):
    snap = tmp_path / 'snap.jpg'
    snap.write_bytes(b'\xff\xd8\xff\xe0')
    s = tr.begin_session(trigger='doorbell')
    tr.current()['snapshot'] = str(snap)
    tr.msg('visitor', 'hello')
    sid = tr.end_session()['session_id']

    async with await client as c:
        r = await c.delete('/api/sessions/' + sid)
        assert r.status == 200
        assert (await r.json()) == {'deleted': sid}

    assert tr.load_session(sid) is None
    assert not snap.exists()


@pytest.mark.asyncio
async def test_delete_session_keeps_other_sessions(client, tmp_path):
    keep = _finished_session(tmp_path, snapshot=False)
    drop = _finished_session(tmp_path, snapshot=True)

    async with await client as c:
        r = await c.delete('/api/sessions/' + drop)
        assert r.status == 200

    assert tr.load_session(drop) is None
    assert tr.load_session(keep) is not None
    assert [s['session_id'] for s in tr.load_history()['sessions']] == [keep]


@pytest.mark.asyncio
async def test_delete_live_session_409(client):
    tr.begin_session(trigger='doorbell')
    live_sid = tr.current()['session_id']
    tr.msg('visitor', 'hello')

    async with await client as c:
        r = await c.delete('/api/sessions/' + live_sid)
        assert r.status == 409


@pytest.mark.asyncio
async def test_delete_all_removes_everything(client, tmp_path):
    for i in range(3):
        _finished_session(tmp_path, snapshot=(i % 2 == 0))

    async with await client as c:
        r = await c.delete('/api/history', json={'count': 3})
        assert r.status == 200
        assert (await r.json()) == {'deleted': 3}
        r2 = await c.get('/api/history')
        assert (await r2.json()) == {'sessions': [], 'total': 0}


@pytest.mark.asyncio
async def test_delete_all_count_mismatch_409(client, tmp_path):
    _finished_session(tmp_path, snapshot=False)

    async with await client as c:
        r = await c.delete('/api/history', json={'count': 5})
        assert r.status == 409
        assert (await r.json()) == {'error': 'count changed; re-confirm', 'count': 1}

    assert tr.load_history()['total'] == 1


@pytest.mark.asyncio
async def test_delete_all_no_body_accepts(client, tmp_path):
    _finished_session(tmp_path, snapshot=False)
    async with await client as c:
        r = await c.delete('/api/history')
        assert r.status == 200
        assert (await r.json()) == {'deleted': 1}
