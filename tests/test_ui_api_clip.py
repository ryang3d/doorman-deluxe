import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
import pytest
import doorman_config as dc
import ui_api
import transcripts as tr
import frigate_clips as fc


@pytest.fixture
def client(aiohttp_client, tmp_path, monkeypatch):
    monkeypatch.setenv('DOORMAN_DATA_DIR', str(tmp_path))
    monkeypatch.setattr(dc, 'ENV_FILE_PATH', str(tmp_path / '.env'))
    app = ui_api.build_app()
    return aiohttp_client(app)


@pytest.mark.asyncio
async def test_clip_404_unknown_session(client):
    ui_api._CLIP_CACHE.clear()
    async with await client as c:
        r = await c.get('/api/clip/no-such-session')
        assert r.status == 404


@pytest.mark.asyncio
async def test_clip_streams_event_clip(client, monkeypatch):
    ui_api._CLIP_CACHE.clear()
    ev = '1700.5-abcd'
    tr.begin_session(trigger='person', frigate_event_id=ev)
    sid = tr.current()['session_id']
    tr.end_session()

    async def fake_resolve(sid_):
        assert sid_ == sid
        return {'event_id': ev, 'clip_url': 'http://x/clip.mp4', 'match': 'exact'}
    monkeypatch.setattr(fc, 'resolve_event_id', fake_resolve)

    MP4 = b'\x00\x00\x00\x18ftypisom' + b'\x00' * 32
    async def fake_get(url, timeout):
        class R:
            status = 200
            headers = {'Content-Type': 'video/mp4'}
            async def read(self):
                return MP4
        return R()
    monkeypatch.setattr(
        'aiohttp.ClientSession.get',
        lambda self, url, timeout=None, **kw: _cm(fake_get(url, timeout)))
    async with await client as c:
        r = await c.get('/api/clip/' + sid)
        assert r.status == 200
        assert r.headers['Content-Type'] == 'video/mp4'
        body = await r.read()
        assert body == MP4
    # resolution is cached in the ui_api module (not frigate_clips)
    assert ui_api._CLIP_CACHE.get(sid) == ev


@pytest.mark.asyncio
async def test_clip_404_when_no_event_resolves(client, monkeypatch):
    ui_api._CLIP_CACHE.clear()
    tr.begin_session(trigger='doorbell')
    sid = tr.current()['session_id']
    tr.end_session()
    async def fake_resolve_none(sid_):
        return None
    monkeypatch.setattr(fc, 'resolve_event_id', fake_resolve_none)
    async with await client as c:
        r = await c.get('/api/clip/' + sid)
        assert r.status == 404


def _cm(coro):
    """Minimal async context manager so `async with ... .get(...)` works."""
    class _C:
        def __init__(self, co):
            self._co = co
        async def __aenter__(self):
            return await self._co
        async def __aexit__(self, *a):
            return False
    return _C(coro)
