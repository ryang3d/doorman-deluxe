#!/usr/bin/env python3
"""Doorman UI backend: aiohttp JSON API + static frontend on the LAN.

Serves / (the frontend) and /api/*. No auth (trusted LAN, per 2026-09-30
decision). Runs in the same process/event loop as the listeners, so it shares
the transcript store and the camera health probe directly.

Endpoints:
  GET  /api/status          health/summary dashboard payload
  GET  /api/history         session summaries, newest first
  GET  /api/sessions/{id}   full session (transcript + metadata)
  DELETE /api/sessions/{id} delete one completed session (+ its snapshot)
  DELETE /api/history       delete all sessions (+ their snapshots)
  GET  /api/live            the in-progress session, if any
  GET  /api/snapshot/{id}   per-session snapshot image
  GET  /api/clip/{id}       Frigate clip matching a session (proxy; 404 if none)
  GET  /api/config          config form schema + current values
  POST /api/config          write .env + hot-apply; reports restart-required
  POST /api/restart         re-exec the process in place (applies restart keys)
"""
import asyncio, json, logging, os, sys, threading, time
from aiohttp import web

import doorman_config as _dc
import doorman_tools as _tools
import ui_schema as _schema
import transcripts as _tr
import prompts_store as _ps
import doorman_prompt as _dp
import frigate_clips as _clips

log = logging.getLogger('doorman.ui')

_PROCESS_START = time.time()
_RESTART_FLAG = {'on': False}
_CLIP_CACHE = {}   # session_id -> resolved event id (avoids re-resolving on repeat views)


def _cfg():
    return _dc.load()


def _version():
    return 'doorman-ui-1.0'


# --------------------------------------------------------------- status
async def _camera_healthy():
    try:
        import audio_bridge as ab
        return bool(await ab.camera_healthy(camera='front_doorbell'))
    except Exception:
        return None   # probe failed -> unknown (UI shows amber)


async def api_status(request):
    cfg = _cfg()
    last = _tr.load_history(limit=1)
    live = _tr.active_session()
    return web.json_response({
        'version': _version(),
        'engine': str(cfg.get('DOORMAN_VOICE_ENGINE', 'gemini')),
        'trigger_mode': str(cfg.get('DOORMAN_TRIGGER_MODE', 'person')),
        'camera_healthy': await _camera_healthy(),
        'in_conversation': live is not None,
        'live_session': live,
        'last_visit': last[0] if last else None,
        'uptime_s': int(time.time() - _PROCESS_START),
    })


# --------------------------------------------------------------- history
async def api_history(request):
    try:
        limit = int(request.query.get('limit', '50'))
        offset = int(request.query.get('offset', '0'))
    except ValueError:
        return web.json_response({'error': 'bad limit/offset'}, status=400)
    return web.json_response(_tr.load_history(limit=limit, offset=offset))


async def api_session(request):
    s = _tr.load_session(request.match_info['id'])
    if s is None:
        return web.json_response({'error': 'not found'}, status=404)
    return web.json_response(s)


async def api_session_delete(request):
    """DELETE /api/sessions/{id} — remove one completed session + snapshot.
    409 while that session is live (not yet persisted), 404 if unknown."""
    sid = request.match_info['id']
    live = _tr.active_session()
    if live and live['session_id'] == sid:
        return web.json_response(
            {'error': 'session is live; delete it after it ends'}, status=409)
    if not _tr.delete_session(sid):
        return web.json_response({'error': 'not found'}, status=404)
    return web.json_response({'deleted': sid})


async def api_history_delete(request):
    """DELETE /api/history — remove all sessions + snapshots.
    Optional JSON body {"count": N}: if given and N != current row count,
    409 so a stale confirm-dialog can't wipe a changed history."""
    expected = None
    try:
        body = await request.json()
        if isinstance(body, dict) and body.get('count') is not None:
            expected = body['count']
    except Exception:
        pass
    count = len(_tr.load_history(limit=10 ** 6, offset=0))
    if expected is not None and expected != count:
        return web.json_response({'error': 'count changed; re-confirm',
                                  'count': count}, status=409)
    return web.json_response({'deleted': _tr.delete_all_sessions()})


async def api_live(request):
    return web.json_response(_tr.active_session())


async def api_snapshot(request):
    s = _tr.load_session(request.match_info['id'])
    if not s or not s.get('snapshot') or not os.path.exists(s['snapshot']):
        return web.json_response({'error': 'no snapshot'}, status=404)
    return web.FileResponse(s['snapshot'])


async def api_clip(request):
    """Stream the Frigate clip matching a session (exact event id first,
    time-overlap fallback). 200 video/mp4; 404 if no matching event with a
    clip; 502 if Frigate is unreachable."""
    sid = request.match_info['id']
    ev = _CLIP_CACHE.get(sid)
    if ev is None:
        try:
            resolved = await _clips.resolve_event_id(sid)
        except Exception as e:
            log.warning('clip resolve error for %s: %s', sid, e)
            return web.json_response({'error': 'clip resolve failed'}, status=502)
        if not resolved:
            return web.json_response({'error': 'no matching frigate event with a clip'},
                                     status=404)
        ev = resolved['event_id']
        _CLIP_CACHE[sid] = ev
    import aiohttp
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(_clip_url(ev),
                             timeout=aiohttp.ClientTimeout(total=60)) as r:
                if r.status != 200:
                    return web.json_response(
                        {'error': 'frigate clip fetch failed'}, status=502)
                body = await r.read()
        return web.Response(body=body,
                            content_type='video/mp4',
                            headers={'Content-Length': str(len(body))})
    except Exception as e:
        log.warning('clip stream error: %s', e)
        return web.json_response({'error': 'frigate clip fetch failed'}, status=502)


def _clip_url(event_id):
    base = _cfg().get('FRIGATE_URL') or ''
    return base.rstrip('/') + '/api/events/' + event_id + '/clip.mp4'


# --------------------------------------------------------------- config
def _current_values():
    """Wire-type values for every field (number->int/float, bool->bool)."""
    cfg = _cfg()
    out = {}
    fm = _schema.field_map()
    for key, spec in fm.items():
        v = cfg.get(key, spec['default'])
        if spec['type'] == 'number':
            try:
                f = float(v)
                v = int(f) if f == int(f) else f
            except (TypeError, ValueError):
                v = spec['default']
        elif spec['type'] == 'boolean':
            v = str(v).strip().lower() in ('true', '1', 'yes', 'on')
        elif isinstance(v, (set, frozenset)):
            # load() normalizes DOORMAN_IGNORED_FACES to a set; the form + .env
            # use a comma-separated string, so round-trip it back to that form.
            v = ','.join(sorted(v))
        elif v is None:
            v = spec['default']
        out[key] = v
    return out


def _write_env_file(updates):
    """Merge updates into the .env file, preserving comments and every other
    line; brand-new keys are appended."""
    path = _dc.ENV_FILE_PATH
    lines = open(path).read().splitlines() if os.path.exists(path) else []
    changed = set(updates.keys())
    out_lines = []
    for line in lines:
        s = line.strip()
        if s and not s.startswith('#') and '=' in s:
            k = s.split('=', 1)[0].strip()
            if k in changed:
                out_lines.append('%s=%s' % (k, updates[k]))
                continue
        out_lines.append(line)
    for k, v in updates.items():
        if not any(l.strip().split('=', 1)[0].strip() == k
                   for l in out_lines
                   if l.strip() and not l.strip().startswith('#') and '=' in l):
            out_lines.append('%s=%s' % (k, v))
    with open(path, 'w') as fh:
        fh.write('\n'.join(out_lines) + '\n')


def _apply_hot():
    """Refresh os.environ from the .env file so the next fresh load() (at the
    point of use) sees the new hot values. No listener rebind needed."""
    _dc.load_env_file(_dc.ENV_FILE_PATH)


async def api_config_get(request):
    vals = _current_values()
    fm = _schema.field_map()
    fields = []
    for f in _schema.FIELDS:
        spec = dict(fm[f[0]])
        spec['value'] = vals.get(f[0])
        if spec['secret'] and spec['value']:
            spec['value'] = '****'   # never echo real secrets
        fields.append(spec)
    return web.json_response({'fields': fields,
                              'groups': _schema.groups_in_order()})


async def api_config_post(request):
    try:
        body = await request.json()
    except Exception:
        return web.json_response({'error': 'bad json'}, status=400)
    fm = _schema.field_map()
    updates = {k: v for k, v in (body.get('values') or {}).items() if k in fm}
    if not updates:
        return web.json_response({'error': 'no known keys'}, status=400)
    # serialize to .env strings
    for k, v in updates.items():
        if v is None:
            updates[k] = ''
        elif fm[k]['type'] == 'boolean':
            updates[k] = 'true' if v else 'false'
        else:
            updates[k] = str(v)
    _write_env_file(updates)
    _apply_hot()
    hot = sorted(k for k in updates if not fm[k]['restart_required'])
    need_restart = sorted(k for k in updates if fm[k]['restart_required'])
    log.info('ui config save: hot=%s restart=%s', hot, need_restart)
    return web.json_response({'applied': hot,
                              'restart_required': need_restart,
                              'needs_restart': bool(need_restart)})


# --------------------------------------------------------------- restart
def _do_restart(argv_extra):
    if _RESTART_FLAG['on']:
        return
    _RESTART_FLAG['on'] = True
    log.info('ui: re-execing to apply restart-required config')
    try:
        sys.stdout.flush()
        sys.stderr.flush()
    except Exception:
        pass
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'doorman.py')
    os.execv(sys.executable, [sys.executable, script] + argv_extra)


async def api_restart(request):
    argv_extra = [a for a in sys.argv[1:]]
    # re-exec ~0.4s after the 200 flushes so the client sees the response
    threading.Timer(0.4, _do_restart, args=(argv_extra,)).start()
    return web.json_response({'ok': True,
                              'message': 'restarting; UI returns in ~2s'})


async def api_restart_method_not_allowed(request):
    # explicit GET so the router answers 405 instead of falling into the
    # catch-all static route (which would 404 the unknown path)
    raise web.HTTPMethodNotAllowed(request.method, {'POST'})


async def api_notify_test(request):
    # Send a real push to the homeowner's devices via the same path the
    # notify_ryan tool uses (HA notify/all_devices). Lets the user verify the
    # notification pipeline end-to-end from the UI.
    try:
        import doorman_tools
        ok, val = await doorman_tools.notify_ryan(
            'Doorman UI: test notification. If your phone buzzed, the '
            'pipeline is healthy.',
            doorman_tools._creds())
        return web.json_response({'ok': bool(ok), 'detail': str(val)})
    except Exception as e:
        return web.json_response({'ok': False, 'detail': str(e)}, status=500)


# --------------------------------------------------------------- prompts
def _prompt_placeholders(key):
    return {
        'system': ['{household_hint}', '{identity}'],
        'animal': ['{animal_label}', '{greeting}'],
        'trigger': ['{facts}'],
        'local_notes': [],
        'animal_cat_lines': [],
        'animal_dog_lines': [],
        'animal_generic_line': [],
    }[key]


def _prompt_payload():
    cur = _ps.load()
    defs = _ps.defaults()
    prompts = {}
    for k in _ps.KEYS:
        prompts[k] = {'text': cur[k], 'default': defs[k],
                      'placeholders': _prompt_placeholders(k)}
    return {'prompts': prompts, 'order': list(_ps.KEYS)}


async def api_prompts_get(request):
    return web.json_response(_prompt_payload())


async def api_prompts_post(request):
    try:
        body = await request.json()
    except Exception:
        return web.json_response({'error': 'bad json'}, status=400)
    if not isinstance(body, dict):
        return web.json_response({'error': 'bad json'}, status=400)
    values = body.get('values') or {}
    if not isinstance(values, dict):
        return web.json_response({'error': 'values must be an object'}, status=400)
    updates = {k: v for k, v in values.items()}
    saved = _ps.save(updates)
    return web.json_response({'saved': saved})


async def api_prompts_reset(request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    key = body.get('key')
    key = key.strip() if isinstance(key, str) else ''
    return web.json_response({'ok': _ps.reset(key)})


async def api_prompts_preview(request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    tpl = body.get('system')
    if not isinstance(tpl, str) or not tpl:
        tpl = _ps.get('system')
    return web.json_response({
        'system': {
            'recognized': _dp.preview_system(tpl, 'Ryan'),
            'unknown': _dp.preview_system(tpl, None),
        }
    })


# --------------------------------------------------------------- app
def build_app():
    app = web.Application()
    app.router.add_get('/api/status', api_status)
    app.router.add_get('/api/history', api_history)
    app.router.add_get('/api/sessions/{id}', api_session)
    app.router.add_delete('/api/sessions/{id}', api_session_delete)
    app.router.add_delete('/api/history', api_history_delete)
    app.router.add_get('/api/live', api_live)
    app.router.add_get('/api/snapshot/{id}', api_snapshot)
    app.router.add_get('/api/clip/{id}', api_clip)
    app.router.add_get('/api/config', api_config_get)
    app.router.add_post('/api/config', api_config_post)
    app.router.add_get('/api/restart', api_restart_method_not_allowed)
    app.router.add_post('/api/restart', api_restart)
    app.router.add_post('/api/notify-test', api_notify_test)
    app.router.add_get('/api/prompts', api_prompts_get)
    app.router.add_post('/api/prompts', api_prompts_post)
    app.router.add_post('/api/prompts/reset', api_prompts_reset)
    app.router.add_post('/api/prompts/preview', api_prompts_preview)
    ui_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'ui')

    async def _index(request):
        # exact '/' -> the app shell (add_static's show_index lists the dir
        # instead of serving index.html, so handle the root explicitly)
        idx = os.path.join(ui_dir, 'index.html')
        if os.path.exists(idx):
            return web.FileResponse(idx)
        return web.Response(status=404, text='index.html not found in src/ui/')

    app.router.add_get('/', _index)
    app.router.add_static('/', ui_dir, name='ui', show_index=False)
    return app


async def serve(port=8090):
    """Start the UI in-process; returns an AppRunner (cleanup on shutdown)."""
    app = build_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    log.info('ui: listening on 0.0.0.0:%d', port)
    return runner
