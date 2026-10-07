import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
import pytest
import transcripts as tr


def test_export_transcript_text_renders_roles_and_time(tmp_path, monkeypatch):
    monkeypatch.setenv('DOORMAN_DATA_DIR', str(tmp_path))
    tr.begin_session(trigger='doorbell', name='Ryan')
    tr.msg('visitor', 'Hello, is Ryan home?')
    tr.msg('doorman', "He's working from home right now.")
    tr.msg('tool', 'notify_ryan', kind='notify')
    rec = tr.end_session()
    text = tr.export_transcript_text(rec)
    assert 'DOORBELL SESSION' in text
    assert 'Ryan' in text
    text_lines = text.splitlines()
    assert any('Visitor' in l and 'Hello, is Ryan home?' in l for l in text_lines)
    assert any('Doorman' in l and "He's working from home" in l for l in text_lines)
    assert any('Tool · notify' in l and 'notify_ryan' in l for l in text_lines)
    # every message got its own line, each starting with a local-time clock
    import re
    msg_lines = [l for l in text_lines if re.match(r'^\d{2}:\d{2} ', l)]
    assert len(msg_lines) == 3


@pytest.mark.asyncio
async def test_load_history_reports_has_clip(tmp_path, monkeypatch):
    import frigate_clips as fc
    monkeypatch.setenv('DOORMAN_DATA_DIR', str(tmp_path))

    tr.begin_session(trigger='doorbell', frigate_event_id='ev-1')
    tr.msg('visitor', 'a')
    sid1 = tr.end_session()['session_id']
    tr.begin_session(trigger='person')
    tr.msg('visitor', 'b')
    sid2 = tr.end_session()['session_id']

    calls = []
    async def fake_has_clip(ev_id):
        calls.append(ev_id)
        return ev_id == 'ev-1'
    monkeypatch.setattr(fc, 'get_event_has_clip', fake_has_clip)

    rows = await tr.load_history_async(limit=10)
    by_id = {r['session_id']: r for r in rows['sessions']}
    assert by_id[sid1]['has_clip'] is True
    assert by_id[sid2]['has_clip'] is False
    assert calls == ['ev-1']   # sessions without an event id are not probed
