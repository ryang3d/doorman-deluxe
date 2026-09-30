#!/usr/bin/env python3
"""Doorman tools (Phase 2, Task 2.3): tools the model can call during a door interaction.

snapshot_front_door()  -> grab a still of the front door camera, save to disk, return path.
notify_ryan(msg)       -> push a notification to the homeowner's devices (notify.all_devices).

Each tool is exposed to Gemini Live as a FunctionDeclaration and dispatched by
run_tool_call(name, args). All HTTP calls are async via aiohttp.
"""
import asyncio, json, os, time, logging, base64

import doorman_config as _dc

log = logging.getLogger("doorman.tools")

# All config comes from doorman_config (env > profile files > defaults).
def _cfg():
    return _dc.load()

def _creds():
    """Return dict with hass creds + frigate_url (snapshot endpoint needs no login)."""
    c = _cfg()
    return {
        'hass_url': c.get('HASS_URL'),
        'hass_token': c.get('HASS_TOKEN'),
        'frigate_url': c.get('FRIGATE_URL'),
    }

def _snapshot_dir():
    """Snapshot output dir from config, guaranteed non-None (falls back to project default)."""
    return _cfg().get('DOORMAN_SNAPSHOT_DIR') or os.path.expanduser('~/doorman/snapshots')


# ---------------------------------------------------------------- function declarations
def tool_declarations():
    """Return the list of FunctionDeclaration objects for the Live session."""
    from google.genai import types
    obj = types.Type.OBJECT
    string = types.Type.STRING
    return [
        types.FunctionDeclaration(
            name='snapshot_front_door',
            description=(
                'Capture a current still image of the front door camera and save it to '
                'disk. Use this when you need a visual of who/what is at the door, e.g. '
                'before notifying the homeowner. Returns the file path of the saved image.'
            ),
            parameters=types.Schema(
                type=obj,
                properties={},
                required=[],
            ),
        ),
        types.FunctionDeclaration(
            name='notify_ryan',
            description=(
                'Send a notification to the homeowner (Ryan) on his phone/tablets. Use '
                'this to alert him about a package delivery, an emergency or urgent '
                'report from a neighbor, a solicitor, or an unrecognized visitor. '
                'Provide a concise message. Returns confirmation.'
            ),
            parameters=types.Schema(
                type=obj,
                properties={
                    'message': types.Schema(
                        type=string,
                        description='Concise notification text for the homeowner, e.g. '
                                    '"Water leak reported behind the house by a neighbor. '
                                    'See snapshot."'
                    ),
                },
                required=['message'],
            ),
        ),
    ]


# ---------------------------------------------------------------- tool implementations
async def _ha_request(cfg, method, path, data=None, raw=False):
    import aiohttp
    url = cfg['hass_url'].rstrip('/') + path
    headers = {'Authorization': 'Bearer ' + cfg['hass_token'],
               'Content-Type': 'application/json'}
    async with aiohttp.ClientSession() as s:
        async with s.request(method, url, headers=headers, json=data, timeout=15) as r:
            if raw:
                body = await r.read()
            else:
                body = await r.text()
            return r.status, body


async def _frigate_snapshot_bytes(cfg):
    """Pull a snapshot image of the front door camera from Frigate, return bytes.
    No auth login needed - the snapshot endpoint is read-only and accessible without
    a session (verified). Falls back to HA camera snapshot if Frigate unavailable."""
    import aiohttp
    frigate = cfg['frigate_url'].rstrip('/')
    # Frigate snapshot of the front_doorbell camera: latest detection image (no login)
    async with aiohttp.ClientSession() as s:
        async with s.get(frigate + '/api/front_doorbell/latest.jpg', timeout=15) as r:
            if r.status == 200:
                return await r.read()
    return None


async def snapshot_front_door(cfg=None):
    """Grab a still of the front door camera, save to snapshot dir, return (ok, path_or_err)."""
    cfg = cfg or _creds()
    sdir = _snapshot_dir()
    try:
        os.makedirs(sdir, exist_ok=True)
        data = await _frigate_snapshot_bytes(cfg)
        source = 'frigate'
        if not data:
            # fallback: HA camera proxy (raw bytes)
            import aiohttp
            status, body = await _ha_request(cfg, 'GET', '/api/camera_proxy/camera.front_doorbell',
                                             raw=True)
            if status == 200 and len(body) > 1000:
                data = body
                source = 'ha'
            else:
                return False, 'snapshot failed: HA camera_proxy status %s' % status
        fname = 'front_door_%s.jpg' % time.strftime('%Y%m%d_%H%M%S')
        path = os.path.join(sdir, fname)
        if isinstance(data, str):
            data = data.encode('latin1')
        with open(path, 'wb') as f:
            f.write(data)
        log.info("snapshot saved: %s (%d bytes, %s)", path, len(data), source)
        _prune_snapshots()
        return True, path
    except Exception as e:
        log.warning("snapshot_front_door error: %s", e)
        return False, 'snapshot error: %s' % e


def _prune_snapshots():
    """Delete the oldest snapshots beyond the retention count (keep the N most recent).

    Retention from config DOORMAN_SNAPSHOT_RETENTION; 0 = keep all / no pruning.
    Called after a new snapshot is saved so the dir never grows unbounded.
    """
    keep = int(_cfg().get('DOORMAN_SNAPSHOT_RETENTION') or 0)
    if keep <= 0:
        return
    sdir = _snapshot_dir()
    try:
        files = [os.path.join(sdir, f) for f in os.listdir(sdir)
                 if f.startswith('front_door_') and f.endswith('.jpg')]
        if len(files) <= keep:
            return
        # oldest first
        files.sort(key=os.path.getmtime)
        removed = 0
        for path in files[:len(files) - keep]:
            try:
                os.remove(path)
                removed += 1
            except OSError:
                pass
        if removed:
            log.info("pruned %d old snapshot(s), kept %d (retention=%d)",
                     removed, keep, keep)
    except Exception as e:
        log.warning("prune snapshots error: %s", e)


# ---------------------------------------------------------------- HA-native snapshot for notifications
async def _ha_snapshot_url(cfg):
    """Capture a frame of the doorbell via HA camera.snapshot and return its /local URL.

    HA writes the jpg to /config/www/doorbell/ (served at /local/doorbell/) and we
    return that URL for the notification image. No container HTTP server, no SSH,
    no advertise-host config: HA captures + serves it regardless of where Doorman runs.
    Returns the URL like http://<ha>/local/doorbell/<file>.jpg or None on failure.
    """
    fname = 'front_door_%s.jpg' % time.strftime('%Y%m%d_%H%M%S')
    ha_path = '/config/www/doorbell/' + fname
    status, body = await _ha_request(
        cfg, 'POST', '/api/services/camera/snapshot',
        {'entity_id': 'camera.front_doorbell', 'filename': ha_path})
    if status not in (200, 201):
        log.warning("camera.snapshot failed: status %s body %s", status, body[:200])
        return None
    hass = (cfg.get('hass_url') or '').rstrip('/')
    url = f"{hass}/local/doorbell/{fname}"
    log.info("HA snapshot saved to %s -> %s", ha_path, url)
    return url


async def notify_ryan(message, cfg=None, image_path=None):
    """Send a push notification to Ryan's devices via HA notify.all_devices.
    Attaches a doorbell frame captured by HA camera.snapshot (served at HA /local),
    so the phone shows the image. image_path is accepted for backward compatibility
    but HA captures its own frame, so a fresh doorbell frame is always attached."""
    cfg = cfg or _creds()
    payload = {'message': message, 'title': 'Doorman'}
    url = await _ha_snapshot_url(cfg)
    if url:
        # mobile_app attachments: data.image = URL
        payload['data'] = {'image': url}
        log.info("notification image url: %s", url)
    status, body = await _ha_request(cfg, 'POST', '/api/services/notify/all_devices', payload)
    if status in (200, 201):
        log.info("notified Ryan: %s", message[:60])
        return True, 'notification sent'
    else:
        log.warning("notify failed: status %s body %s", status, body[:200])
        return False, 'notify failed: %s' % status


# ---------------------------------------------------------------- dispatcher
async def run_tool_call(call, cfg=None):
    """Execute a Gemini FunctionCall; return a dict {'result': ...} for send_tool_response.
    call: google.genai.types.FunctionCall"""
    name = getattr(call, 'name', '')
    args = getattr(call, 'args', {}) or {}
    cfg = cfg or _creds()
    log.info("tool call: %s args=%s", name, json.dumps(args)[:200])
    if name == 'snapshot_front_door':
        ok, val = await snapshot_front_door(cfg)
        return {'result': json.dumps({'ok': ok, 'value': val})}
    elif name == 'notify_ryan':
        ok, val = await notify_ryan(str(args.get('message', '')), cfg)
        return {'result': json.dumps({'ok': ok, 'value': val})}
    else:
        return {'result': json.dumps({'ok': False, 'value': 'unknown tool ' + name})}
