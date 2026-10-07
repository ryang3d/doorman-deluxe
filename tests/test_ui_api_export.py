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


@pytest.mark.asyncio
async def test_export_all_two_sessions(client, tmp_path, monkeypatch):
    sid_a = _finished(tmp_path, snapshot=True, event_id='ev-a')
    sid_b = _finished(tmp_path, snapshot=False, event_id=None)
    _stub_clip(monkeypatch, sid_a, 'ev-a')

    async def fake_resolve_all(s_):
        return {'event_id': 'ev-a', 'clip_url': 'http://x/clip.mp4',
                'match': 'exact'} if s_ == sid_a else None
    monkeypatch.setattr(fc, 'resolve_event_id', fake_resolve_all)

    async with await client as c:
        r = await c.post('/api/sessions/export-all?parts=transcript,snapshot,clip')
        assert r.status == 200
        z = _zip(await r.read())
        expected = sorted(
            [sid_a + '/' + sid_a + '.transcript.txt',
             sid_a + '/' + sid_a + '.session.json',
             sid_a + '/snapshot.jpg',
             sid_a + '/clip.mp4',
             sid_b + '/' + sid_b + '.transcript.txt',
             sid_b + '/' + sid_b + '.session.json'])
        assert sorted(z.namelist()) == expected


@pytest.mark.asyncio
async def test_export_all_empty_history_404(client):
    async with await client as c:
        r = await c.post('/api/sessions/export-all?parts=transcript')
        assert r.status == 404


@pytest.mark.asyncio
async def test_export_all_unknown_part_400(client, tmp_path):
    _finished(tmp_path, snapshot=False)
    async with await client as c:
        r = await c.post('/api/sessions/export-all?parts=bogus')
        assert r.status == 400


@pytest.mark.asyncio
async def test_export_all_ids_filters(client, tmp_path):
    # ids= restricts the export to a subset of session ids (multi-select export)
    sid_a = _finished(tmp_path, snapshot=False)
    sid_b = _finished(tmp_path, snapshot=False)
    async with await client as c:
        r = await c.post('/api/sessions/export-all?parts=transcript&ids=' + sid_a)
        assert r.status == 200
        z = _zip(await r.read())
        assert sorted(z.namelist()) == [sid_a + '/' + sid_a + '.session.json',
                                        sid_a + '/' + sid_a + '.transcript.txt']
        # multiple ids, comma-separated; order-preserving, unknown ids ignored
        r2 = await c.post('/api/sessions/export-all?parts=transcript&ids='
                          + sid_b + ',nope,' + sid_a)
        assert r2.status == 200
        z2 = _zip(await r2.read())
        top = {n.split('/')[0] for n in z2.namelist()}
        assert top == {sid_a, sid_b}


@pytest.mark.asyncio
async def test_export_all_ids_empty_404(client, tmp_path):
    _finished(tmp_path, snapshot=False)
    async with await client as c:
        r = await c.post('/api/sessions/export-all?parts=transcript&ids=nope')
        assert r.status == 404


def _make_zip_for_import(sid, snapshot=True):
    """Build a doorman-style zip (same layout as the export) for import tests.
    Uses a real transcripts session so the JSONL shape is genuine, forces the
    session_id, then deletes the helper's own row so the JSONL is clean for
    the import assertion to follow."""
    import zipfile
    real = tr.begin_session(trigger='doorbell')
    tr.msg('visitor', 'imported')
    tr.msg('doorman', 'welcome')
    rec = tr.end_session()
    rec = dict(rec)
    rec['session_id'] = sid          # force the id the importer should use
    rec['is_doorman_session'] = True
    tr.delete_session(real['session_id'])   # don't leave the helper's row behind
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as z:
        z.writestr(sid + '.transcript.txt', 'DOORBELL SESSION ' + sid + '\n')
        z.writestr(sid + '.session.json',
                   json.dumps(rec, ensure_ascii=False))
        if snapshot:
            z.writestr('snapshot.jpg', b'\xff\xd8\xff\xe0IMP')
    return buf.getvalue()


@pytest.mark.asyncio
async def test_import_zip_creates_session_and_snapshot(client, tmp_path):
    zdata = _make_zip_for_import('imp-0001')
    async with await client as c:
        import aiohttp
        form = aiohttp.FormData()
        form.add_field('file', zdata, filename='doorman-imp-0001.zip')
        r = await c.post('/api/sessions/import', data=form)
        assert r.status == 200
        body = await r.json()
        assert body['imported'] == ['imp-0001']
    s = tr.load_session('imp-0001')
    assert s is not None
    assert s['messages'][0]['text'] == 'imported'
    assert s['is_doorman_session'] is True
    assert s.get('snapshot', '').endswith('imp-0001.jpg')
    assert os.path.exists(s['snapshot'])
    assert open(s['snapshot'], 'rb').read() == b'\xff\xd8\xff\xe0IMP'
    assert [x['session_id'] for x in tr.load_history()['sessions']] == ['imp-0001']


@pytest.mark.asyncio
async def test_import_duplicate_conflict_409(client, tmp_path):
    zdata = _make_zip_for_import('imp-0002')
    import aiohttp
    async with await client as c:
        form = aiohttp.FormData()
        form.add_field('file', zdata, filename='a.zip')
        r = await c.post('/api/sessions/import', data=form)
        assert r.status == 200
        form2 = aiohttp.FormData()
        form2.add_field('file', zdata, filename='a.zip')
        r2 = await c.post('/api/sessions/import', data=form2)
        assert r2.status == 409
        body = await r2.json()
        assert body['error'] == 'sessions already exist'
        assert body['conflicts'] == ['imp-0002']
    assert tr.load_session('imp-0002')['messages'][0]['text'] == 'imported'


@pytest.mark.asyncio
async def test_import_duplicate_overwrite(client, tmp_path):
    zdata = _make_zip_for_import('imp-0003', snapshot=False)
    import aiohttp
    async with await client as c:
        form = aiohttp.FormData()
        form.add_field('file', zdata, filename='a.zip')
        r = await c.post('/api/sessions/import', data=form)
        assert r.status == 200
        form2 = aiohttp.FormData()
        form2.add_field('file', zdata, filename='a.zip')
        form2.add_field('overwrite', 'true')
        r2 = await c.post('/api/sessions/import', data=form2)
        assert r2.status == 200
        assert (await r2.json())['imported'] == ['imp-0003']
    # exactly one row, not two
    assert tr.load_history()['total'] == 1


@pytest.mark.asyncio
async def test_import_no_session_json_400(client):
    import aiohttp, zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as z:
        z.writestr('notes.txt', 'no sessions here')
    async with await client as c:
        form = aiohttp.FormData()
        form.add_field('file', buf.getvalue(), filename='other.zip')
        r = await c.post('/api/sessions/import', data=form)
        assert r.status == 400


@pytest.mark.asyncio
async def test_import_bad_zip_400(client):
    import aiohttp
    async with await client as c:
        form = aiohttp.FormData()
        form.add_field('file', b'this is not a zip', filename='x.zip')
        r = await c.post('/api/sessions/import', data=form)
        assert r.status == 400
