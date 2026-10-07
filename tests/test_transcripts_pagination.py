import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
import doorman_config as dc
import transcripts as tr


def _use_tmp_dir(d):
    os.environ['DOORMAN_DATA_DIR'] = d


def _make(n):
    for i in range(n):
        tr.begin_session(name='S%02d' % i)
        tr.msg('visitor', 'hi')
        tr.end_session()


def test_load_history_total(tmp_path):
    _use_tmp_dir(str(tmp_path))
    _make(4)
    d = tr.load_history(limit=5, offset=0)
    assert d['total'] == 4
    assert len(d['sessions']) == 4


def test_load_history_total_is_unaffected_by_limit(tmp_path):
    _use_tmp_dir(str(tmp_path))
    _make(4)
    d = tr.load_history(limit=1, offset=1)
    assert d['total'] == 4
    assert len(d['sessions']) == 1
    assert d['sessions'][0]['recognized_name'] == 'S02'   # newest-first, skip one


def test_load_history_offset_past_end(tmp_path):
    _use_tmp_dir(str(tmp_path))
    _make(4)
    d = tr.load_history(limit=5, offset=9)
    assert d['total'] == 4
    assert d['sessions'] == []
