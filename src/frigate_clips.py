#!/usr/bin/env python3
"""Resolve Doorman sessions to Frigate events / clips.

Exact path: a session's stored frigate_event_id (the triggering event).
Fallback: the recent front-door events list, matched by time overlap with
the session start — covers sessions recorded before frigate_event_id
existed, doorbell-press-triggered sessions (event id None at the edge),
and the case where the triggering event has no clip yet (a sibling event
from the same visit usually does).

All Frigate calls use DOORMAN_FRIGATE_URL; no login needed (verified on
Frigate 0.18.0: /api/events and /api/events/{id}/clip.mp4 answer without a
cookie). The events endpoint takes ?camera=<name>&limit=<n>; results are
newest-first. There is no timestamp filter (?after/ ?before expect event
ids), so the fallback filters in-process by start_time.
"""
import json
from datetime import datetime

import aiohttp

import doorman_config as _dc

_CLIPS = {'limit': 200}   # bounded fetch; ~13 days of front-door events


def _base():
    c = _dc.load()
    return (c.get('FRIGATE_URL') or '').rstrip('/')


def _camera():
    return _dc.load().get('FRONT_CAMERA') or 'front_doorbell'


def _fallback_window_s():
    try:
        return int(float(_dc.load().get('DOORMAN_CLIP_FALLBACK_WINDOW_S') or 300))
    except (TypeError, ValueError):
        return 300


def _parse_dt(iso):
    try:
        return datetime.fromisoformat(iso)
    except (TypeError, ValueError):
        return None


async def _fetch_json(session, url, timeout=15):
    async with session.get(url, timeout=aiohttp.ClientTimeout(total=timeout)) as r:
        if r.status != 200:
            return None
        return await r.json(content_type=None)


async def get_event_has_clip(event_id):
    """True if the event exists and has_clip is true; else False. Never raises."""
    if not event_id:
        return False
    try:
        async with aiohttp.ClientSession() as s:
            data = await _fetch_json(s, _base() + '/api/events/' + event_id)
        return bool(data and data.get('has_clip'))
    except Exception:
        return False


def _score(event, started):
    """Higher is better: overlap with [started, started+60s]; else closeness."""
    st, en = event.get('start_time'), event.get('end_time')
    if st is None:
        return -1
    s_ts = started.timestamp()
    s_end = s_ts + 60.0
    overlap = min(en or st, s_end) - max(st, s_ts)
    if overlap > 0:
        return (1, overlap)
    return (0, -abs(st - s_ts))


async def resolve_event_id(session_id):
    """Return {'event_id', 'clip_url', 'match'} or None. Never raises."""
    import transcripts as _tr
    s = _tr.load_session(session_id)
    if not s:
        return None
    started = _parse_dt(s.get('started_at'))
    if not started:
        return None
    exact = s.get('frigate_event_id')
    if exact and await get_event_has_clip(exact):
        return {'event_id': exact,
                'clip_url': _base() + '/api/events/' + exact + '/clip.mp4',
                'match': 'exact'}
    # Fallback: recent events, best time overlap, prefer events with a clip.
    try:
        async with aiohttp.ClientSession() as a:
            data = await _fetch_json(
                a, _base() + '/api/events?limit=%d&camera=%s'
                % (_CLIPS['limit'], _camera()))
    except Exception:
        return None
    if not isinstance(data, list):
        return None
    cands = []
    for e in data:
        if not e.get('has_clip'):
            continue
        st = e.get('start_time')
        if st is None:
            continue
        if abs(st - started.timestamp()) > _fallback_window_s():
            continue
        cands.append(e)
    if not cands:
        return None
    best = max(cands, key=lambda e: _score(e, started))
    return {'event_id': best['id'],
            'clip_url': _base() + '/api/events/' + best['id'] + '/clip.mp4',
            'match': 'time'}
