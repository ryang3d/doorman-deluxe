import os, sys, json, tempfile
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
import doorman_config as dc
import transcripts as tr

def _use_tmp_dir(d):
    os.environ['DOORMAN_DATA_DIR'] = d

def test_begin_msg_end_roundtrip(tmp_path):
    _use_tmp_dir(str(tmp_path))
    tr.begin_session(trigger='person', name='Ryan', engine='gemini')
    tr.msg('doorman', 'Hello, how can I help?')
    tr.msg('visitor', "I'm here for a package.")
    tr.msg('tool', 'tool: snapshot_front_door', kind='tool')
    s = tr.end_session()
    assert s['status'] == 'ended' and s['snapshot'] is None
    assert [m['role'] for m in s['messages']] == ['doorman', 'visitor', 'tool']
    assert s['duration_s'] is not None

def test_active_session_none_when_idle(tmp_path):
    _use_tmp_dir(str(tmp_path)); tr.end_session()
    assert tr.active_session() is None

def test_history_most_recent_first(tmp_path):
    _use_tmp_dir(str(tmp_path))
    tr.begin_session(name='A'); tr.msg('visitor', 'hi'); tr.end_session()
    tr.begin_session(name='B'); tr.msg('visitor', 'hi'); tr.end_session()
    hist = tr.load_history()
    assert [h['recognized_name'] for h in hist] == ['B', 'A']
    assert hist[0]['message_count'] == 1

def test_capture_session_snapshot_writes_file_and_sets_field(tmp_path, monkeypatch):
    """capture_session_snapshot must fetch via the creds shape (FRIGATE_URL), not the
    raw DOORMAN_* config - the old code passed ab.load_config() into
    _frigate_snapshot_bytes which reads cfg['frigate_url'] -> KeyError -> silent None."""
    _use_tmp_dir(str(tmp_path))
    import doorman_tools as dt
    import asyncio

    captured = {}
    async def fake_snapshot(creds):
        captured.update(creds)
        return b'\xff\xd8fake-jpeg'
    monkeypatch.setattr(dt, '_frigate_snapshot_bytes', fake_snapshot)

    s = tr.begin_session(trigger='doorbell', name='Ryan', engine='local')
    path = asyncio.run(tr.capture_session_snapshot(None, s['session_id']))
    assert path and os.path.exists(path)
    assert open(path, 'rb').read() == b'\xff\xd8fake-jpeg'
    assert tr.current()['snapshot'] == path
    # the snapshot function received the creds dict (frigate_url shape), not a None
    assert 'frigate_url' in captured
    tr.end_session()   # keep isolation: don't leave a live session for other tests
