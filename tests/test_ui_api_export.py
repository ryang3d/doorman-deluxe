import io, json, os, sys, zipfile
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
import pytest
import doorman_config as dc
import ui_api
import transcripts as tr
import frigate_clips as fc


@pytest.fixture(autouse=True)
def _clear_state():
    tr._CURRENT = None
    tr._START_MONO = None
    ui_api._CLIP_CACHE.clear()
    yield
    tr._CURRENT = None
    tr._START_MONO = None
    ui_api._CLIP_CACHE.clear()


@pytest.fixture
def client(aiohttp_client, tmp_path, monkeypatch):
    monkeypatch.setenv('DOORMAN_DATA_DIR', str(tmp_path))
    monkeypatch.setattr(dc, 'ENV_FILE_PATH', str(tmp_path / '.env'))
    app = ui_api.build_app()
    return aiohttp_client(app)


def _finished(tmp_path, snapshot=True, event_id=None):
    s = tr.begin_session(trigger='doorbell', frigate_event_id=event_id)
    if snapshot:
        snap = tmp_path / (s['session_id'] + '.jpg')
        snap.write_bytes(b'\xff\xd8\xff\xe0JPG')
        tr.current()['snapshot'] = str(snap)
    tr.msg('visitor', 'hello')
    tr.msg('doorman', 'hi')
    return tr.end_session()['session_id']


def _zip(r):
    data = r if isinstance(r, bytes) else r.read()
    return zipfile.ZipFile(io.BytesIO(data))


def _stub_clip(monkeypatch, sid, event_id, available=True):
    async def fake_resolve(s_):
        assert s_ == sid
        return {'event_id': event_id, 'clip_url': 'http://x/clip.mp4', 'match': 'exact'} \
            if available else None
    monkeypatch.setattr(fc, 'resolve_event_id', fake_resolve)
    if available:
        MP4 = b'\x00\x00\x00\x18ftypisom' + b'\x00' * 32
        async def fake_get(url, timeout=None, **kw):
            class R:
                status = 200
                async def read(self):
                    return MP4
            return R()
        monkeypatch.setattr('aiohttp.ClientSession.get',
                            lambda self, url, timeout=None, **kw: _cm(fake_get(url, timeout)))


def _cm(coro):
    class _C:
        def __init__(self, co):
            self._co = co
        async def __aenter__(self):
            return await self._co
        async def __aexit__(self, *a):
            return False
    return _C(coro)


@pytest.mark.asyncio
async def test_export_unknown_session_404(client):
    async with await client as c:
        r = await c.post('/api/sessions/no-such/export?parts=transcript')
        assert r.status == 404


@pytest.mark.asyncio
async def test_export_live_session_409(client):
    tr.begin_session(trigger='doorbell')
    live = tr.current()['session_id']
    async with await client as c:
        r = await c.post('/api/sessions/' + live + '/export?parts=transcript')
        assert r.status == 409


@pytest.mark.asyncio
async def test_export_transcript_only(client, tmp_path):
    sid = _finished(tmp_path, snapshot=False)
    async with await client as c:
        r = await c.post('/api/sessions/' + sid + '/export?parts=transcript')
        assert r.status == 200
        assert r.headers['Content-Type'] == 'application/zip'
        assert 'filename="doorman-' + sid + '.zip"' in r.headers.get('Content-Disposition', '')
        z = _zip(await r.read())
        names = z.namelist()
        assert names == [sid + '.transcript.txt', sid + '.session.json']
        meta = json.loads(z.read(sid + '.session.json'))
        assert meta['session_id'] == sid
        assert meta['is_doorman_session'] is True
        assert meta['messages'][0]['text'] == 'hello'
        body = z.read(sid + '.transcript.txt').decode('utf-8')
        assert 'DOORBELL SESSION ' + sid in body
        assert 'hello' in body


@pytest.mark.asyncio
async def test_export_with_snapshot_and_clip(client, tmp_path, monkeypatch):
    ev = '1700.5-abcd'
    sid = _finished(tmp_path, snapshot=True, event_id=ev)
    _stub_clip(monkeypatch, sid, ev)
    async with await client as c:
        r = await c.post('/api/sessions/' + sid + '/export?parts=transcript,snapshot,clip')
        assert r.status == 200
        z = _zip(await r.read())
        names = sorted(z.namelist())
        assert names == sorted(['clip.mp4', 'snapshot.jpg', sid + '.session.json',
                                sid + '.transcript.txt'])
        assert z.read('snapshot.jpg') == b'\xff\xd8\xff\xe0JPG'
        assert z.read('clip.mp4') == b'\x00\x00\x00\x18ftypisom' + b'\x00' * 32


@pytest.mark.asyncio
async def test_export_missing_optional_parts_reported(client, tmp_path, monkeypatch):
    # no snapshot on the session, Frigate resolves nothing
    sid = _finished(tmp_path, snapshot=False)
    _stub_clip(monkeypatch, sid, 'nope', available=False)
    async with await client as c:
        r = await c.post('/api/sessions/' + sid + '/export?parts=transcript,snapshot,clip')
        assert r.status == 200
        z = _zip(await r.read())
        assert sorted(z.namelist()) == [sid + '.session.json', sid + '.transcript.txt']


@pytest.mark.asyncio
async def test_export_unknown_part_400(client, tmp_path):
    sid = _finished(tmp_path, snapshot=False)
    async with await client as c:
        r = await c.post('/api/sessions/' + sid + '/export?parts=transcript,nope')
        assert r.status == 400


@pytest.mark.asyncio
async def test_post_export_id_is_not_import_route(client, tmp_path):
    # POST /api/sessions/{id}/export must not shadow POST /api/sessions/import
    sid = _finished(tmp_path, snapshot=False)
    async with await client as c:
        r = await c.post('/api/sessions/import/export?parts=transcript')
        assert r.status == 404
