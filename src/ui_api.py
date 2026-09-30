#!/usr/bin/env python3
"""Doorman UI backend: aiohttp JSON API + static frontend on the LAN.

Serves / (the frontend) and /api/*. No auth (trusted LAN, per 2026-09-30
decision). Runs in the same process/event loop as the listeners, so it shares
the transcript store and the camera health probe directly.

Endpoints:
  GET  /api/status          health/summary dashboard payload
  GET  /api/history         session summaries, newest first
  GET  /api/sessions/{id}   full session (transcript + metadata)
  GET  /api/live            the in-progress session, if any
  GET  /api/snapshot/{id}   per-session snapshot image
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

log = logging.getLogger('doorman.ui')

_PROCESS_START = time.time()
_RESTART_FLAG = {'on': False}


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


async def api_live(request):
    return web.json_response(_tr.active_session())


async def api_snapshot(request):
    s = _tr.load_session(request.match_info['id'])
    if not s or not s.get('snapshot') or not os.path.exists(s['snapshot']):
        return web.json_response({'error': 'no snapshot'}, status=404)
    return web.FileResponse(s['snapshot'])


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


# --------------------------------------------------------------- app
def build_app():
    app = web.Application()
    app.router.add_get('/api/status', api_status)
    app.router.add_get('/api/history', api_history)
    app.router.add_get('/api/sessions/{id}', api_session)
    app.router.add_get('/api/live', api_live)
    app.router.add_get('/api/snapshot/{id}', api_snapshot)
    app.router.add_get('/api/config', api_config_get)
    app.router.add_post('/api/config', api_config_post)
    app.router.add_get('/api/restart', api_restart_method_not_allowed)
    app.router.add_post('/api/restart', api_restart)
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
