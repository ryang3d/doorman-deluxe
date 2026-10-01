import sys, os, asyncio
from datetime import datetime, timezone, timedelta
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
import doorman_config as dc
import frigate_clips as fc
import transcripts as tr


def _use_tmp_dir(d):
    os.environ['DOORMAN_DATA_DIR'] = d


def _now():
    return datetime.now(timezone.utc)


def test_resolve_exact_match(tmp_path, monkeypatch):
    _use_tmp_dir(str(tmp_path))
    ev = '1700.5-abcd'
    tr.begin_session(trigger='person', frigate_event_id=ev)
    sid = tr.current()['session_id']
    tr.end_session()
    async def fake_has_clip(eid):
        return eid == ev
    monkeypatch.setattr(fc, 'get_event_has_clip', fake_has_clip)
    out = asyncio.run(fc.resolve_event_id(sid))
    assert out == {'event_id': ev,
                   'clip_url': fc._base() + '/api/events/' + ev + '/clip.mp4',
                   'match': 'exact'}


def test_resolve_time_fallback_when_exact_missing(tmp_path, monkeypatch):
    _use_tmp_dir(str(tmp_path))
    ev_now = '1700.5-abcd'
    s = tr.begin_session(trigger='person', frigate_event_id=ev_now)
    sid = s['session_id']
    tr.end_session()
    now = _now()
    ev_other = '1700.6-zzzz'
    start_ts = (now - timedelta(seconds=5)).timestamp()
    async def fake_fetch_json(session, url, timeout=15):
        assert '/api/events?limit=' in url
        assert 'camera=front_doorbell' in url
        return [{'id': ev_other, 'has_clip': True,
                 'start_time': start_ts, 'end_time': start_ts + 25},
                {'id': 'old-1', 'has_clip': True,
                 'start_time': (now - timedelta(days=2)).timestamp(),
                 'end_time': (now - timedelta(days=2)).timestamp() + 20},
                {'id': 'nolip', 'has_clip': False,
                 'start_time': start_ts, 'end_time': start_ts + 20}]
    monkeypatch.setattr(fc, '_fetch_json', fake_fetch_json)
    async def fake_has_clip(eid):
        return False   # force the fallback path
    monkeypatch.setattr(fc, 'get_event_has_clip', fake_has_clip)
    out = asyncio.run(fc.resolve_event_id(sid))
    assert out and out['event_id'] == ev_other, out
    assert out['match'] == 'time'
    assert out['clip_url'].endswith('/api/events/%s/clip.mp4' % ev_other)


def test_resolve_none_when_nothing_in_window(tmp_path, monkeypatch):
    _use_tmp_dir(str(tmp_path))
    s = tr.begin_session(trigger='person', frigate_event_id=None)
    sid = s['session_id']
    tr.end_session()
    now = _now()
    async def fake_fetch_json(session, url, timeout=15):
        ts = (now - timedelta(hours=5)).timestamp()
        return [{'id': 'x', 'has_clip': True, 'start_time': ts,
                 'end_time': ts + 10}]
    monkeypatch.setattr(fc, '_fetch_json', fake_fetch_json)
    monkeypatch.setattr(fc, 'get_event_has_clip', lambda eid: False)
    assert asyncio.run(fc.resolve_event_id(sid)) is None


def test_get_event_has_clip_false_on_bad_id(tmp_path, monkeypatch):
    _use_tmp_dir(str(tmp_path))
    async def fake_fetch_json(session, url, timeout=15):
        return None
    monkeypatch.setattr(fc, '_fetch_json', fake_fetch_json)
    assert asyncio.run(fc.get_event_has_clip('9.9-bad')) is False
    assert asyncio.run(fc.get_event_has_clip(None)) is False
