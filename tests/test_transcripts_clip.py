import os, sys, json
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
import doorman_config as dc
import transcripts as tr


def _use_tmp_dir(d):
    os.environ['DOORMAN_DATA_DIR'] = d


def test_session_has_frigate_event_id_field(tmp_path):
    _use_tmp_dir(str(tmp_path))
    s = tr.begin_session(trigger='person', name='Ryan', engine='gemini',
                         frigate_event_id='123.4-abcd')
    assert s['frigate_event_id'] == '123.4-abcd'
    tr.end_session()

def test_session_frigate_event_id_defaults_none(tmp_path):
    _use_tmp_dir(str(tmp_path))
    s = tr.begin_session(trigger='doorbell')
    assert s['frigate_event_id'] is None
    tr.end_session()

def test_load_history_includes_event_id(tmp_path):
    _use_tmp_dir(str(tmp_path))
    tr.begin_session(trigger='person', frigate_event_id='1.1-a')
    tr.end_session()
    row = tr.load_history()['sessions'][0]
    assert row['frigate_event_id'] == '1.1-a'

def test_load_session_roundtrips_event_id(tmp_path):
    _use_tmp_dir(str(tmp_path))
    s = tr.begin_session(trigger='person', frigate_event_id='2.2-b')
    sid = s['session_id']
    tr.end_session()
    assert tr.load_session(sid)['frigate_event_id'] == '2.2-b'

def test_clip_fallback_window_default(monkeypatch, tmp_path):
    _use_tmp_dir(str(tmp_path))
    monkeypatch.delenv('DOORMAN_CLIP_FALLBACK_WINDOW_S', raising=False)
    cfg = dc.load()
    assert cfg['DOORMAN_CLIP_FALLBACK_WINDOW_S'] == 300
