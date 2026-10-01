#!/usr/bin/env python3
"""Doorman conversation transcripts: in-memory current session + JSONL history.

One process, one asyncio loop, and interactions are serialized (handle_event
awaits run_interaction, triggers are cooldown-debounced), so a single
module-level "current session" is unambiguous. Both the Gemini loop
(doorman.py) and the local loop (voice_local.py) call begin/msg/end at the
same spots they already log. Completed sessions append one JSON object per line
to <DOORMAN_DATA_DIR>/transcripts/sessions.jsonl.
"""
import json, os, time
from datetime import datetime, timezone

import doorman_config as _dc

_CURRENT = None      # active session dict, or None
_START_MONO = None   # monotonic start time of the active session (for duration)

def _data_dir():
    return (_dc.load().get('DOORMAN_DATA_DIR') or '/data')

def _jsonl_path():
    d = os.path.join(_data_dir(), 'transcripts')
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, 'sessions.jsonl')

def _now_iso():
    return datetime.now(timezone.utc).isoformat()

def begin_session(trigger='person', name=None, engine='gemini',
                  doorbell=False, label=None, frigate_event_id=None):
    """Start a session; returns the session dict. Idempotent-ish: overwrites."""
    global _CURRENT, _START_MONO
    _CURRENT = {
        'session_id': time.strftime('%Y%m%d-%H%M%S') + '-' +
                      '%04x' % (time.time_ns() % 65536),
        'started_at': _now_iso(),
        'trigger': trigger,
        'label': label,
        'doorbell': bool(doorbell),
        'recognized_name': name,
        'engine': engine,
        'snapshot': None,
        'frigate_event_id': frigate_event_id,
        'messages': [],
        'status': 'active',
        'ended_at': None,
        'duration_s': None,
    }
    _START_MONO = time.monotonic()
    return _CURRENT

def current():
    return _CURRENT

def active_session():
    """Snapshot of the live session (for the UI) or None."""
    if _CURRENT is None:
        return None
    s = dict(_CURRENT)
    s['messages'] = list(_CURRENT['messages'])
    s['message_count'] = len(_CURRENT['messages'])
    return s

def msg(role, text, kind=None):
    """Append one transcript line. role: visitor|doorman|tool|system."""
    if _CURRENT is None:
        return
    _CURRENT['messages'].append({'ts': _now_iso(), 'role': role,
                                 'text': text, 'kind': kind})

def end_session(status='ended', error=None):
    """Finalize + persist the current session, then clear it."""
    global _CURRENT, _START_MONO
    if _CURRENT is None:
        return None
    _CURRENT['ended_at'] = _now_iso()
    _CURRENT['status'] = status
    if error:
        _CURRENT['error'] = error
    # Record elapsed wall time (monotonic) so the UI can show a duration.
    if _START_MONO is not None:
        _CURRENT['duration_s'] = round(time.monotonic() - _START_MONO, 3)
        _START_MONO = None
    s = _CURRENT
    _CURRENT = None
    try:
        with open(_jsonl_path(), 'a') as f:
            f.write(json.dumps(s, ensure_ascii=False) + '\n')
    except Exception:
        pass
    return s

def load_history(limit=50, offset=0):
    """Most-recent-first list of session summaries."""
    p = _jsonl_path()
    rows = []
    if os.path.exists(p):
        with open(p) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    o = json.loads(line)
                except Exception:
                    continue
                rows.append({
                    'session_id': o.get('session_id'),
                    'started_at': o.get('started_at'),
                    'trigger': o.get('trigger'),
                    'recognized_name': o.get('recognized_name'),
                    'engine': o.get('engine'),
                    'duration_s': o.get('duration_s'),
                    'message_count': len(o.get('messages', [])),
                    'has_snapshot': bool(o.get('snapshot')),
                    'frigate_event_id': o.get('frigate_event_id'),
                })
    rows.sort(key=lambda r: r['started_at'] or '', reverse=True)
    return rows[offset:offset + limit]

def load_session(session_id):
    p = _jsonl_path()
    if not os.path.exists(p):
        return None
    with open(p) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                o = json.loads(line)
            except Exception:
                continue
            if o.get('session_id') == session_id:
                return o
    return None

async def capture_session_snapshot(cfg, session_id):
    """Best-effort thumbnail at session start; never raises, never blocks the
    greeting. Writes to <DOORMAN_DATA_DIR>/snapshots/<id>.jpg."""
    try:
        import doorman_tools
        # _frigate_snapshot_bytes wants the creds shape (cfg['frigate_url']), not
        # the raw DOORMAN_* config that ab.load_config() returns. Build it the
        # same way the model's snapshot tool does.
        data = await doorman_tools._frigate_snapshot_bytes(doorman_tools._creds())
        if not data:
            return None
        d = os.path.join(_data_dir(), 'snapshots')
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, session_id + '.jpg')
        if isinstance(data, str):
            data = data.encode('latin1')
        with open(path, 'wb') as f:
            f.write(data)
        if _CURRENT is not None:
            _CURRENT['snapshot'] = path
        return path
    except Exception:
        return None
