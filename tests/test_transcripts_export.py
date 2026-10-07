import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
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
