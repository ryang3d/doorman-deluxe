#!/usr/bin/env python3
"""Doorman tools (Phase 2, Task 2.3): tools the model can call during a door interaction.

snapshot_front_door()  -> grab a still of the front door camera, save to disk, return path.
notify_ryan(msg)       -> push a notification to the homeowner's devices (notify.all_devices).

Each tool is exposed to Gemini Live as a FunctionDeclaration and dispatched by
run_tool_call(name, args). All HTTP calls are async via aiohttp.
"""
import asyncio, json, os, time, logging, base64

log = logging.getLogger("doorman.tools")

# Config file with HA + Frigate creds (reuse the existing one; it has FRIGATE_*; HASS_* is in .env)
HASS_ENV = '~/.hermes/profiles/home-admin/.env'
FRIGATE_ENV = '~/.hermes/profiles/home-admin/frigate.env'

# Where snapshots are saved (web-served so notify can attach it). Keep under project.
SNAPSHOT_DIR = '~/doorman/snapshots'


def _getenv(path, key):
    for line in open(path):
        line = line.strip()
        if line.startswith(key + '='):
            return line.split('=', 1)[1].strip().strip('"').strip("'")
    return None


def _creds():
    """Return dict with hass_url, hass_token, frigate_url, frigate_user, frigate_password."""
    return {
        'hass_url': _getenv(HASS_ENV, 'HASS_URL'),
        'hass_token': _getenv(HASS_ENV, 'HASS_TOKEN'),
        'frigate_url': _getenv(FRIGATE_ENV, 'FRIGATE_URL'),
        'frigate_user': _getenv(FRIGATE_ENV, 'FRIGATE_USER'),
        'frigate_password': _getenv(FRIGATE_ENV, 'FRIGATE_PASSWORD'),
    }


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
    """Pull a snapshot image of the front door camera from Frigate (authed), return bytes.
    Falls back to HA camera snapshot if Frigate unavailable."""
    import aiohttp
    frigate = cfg['frigate_url'].rstrip('/')
    # login for cookie
    async with aiohttp.ClientSession() as s:
        async with s.post(frigate + '/api/login',
                          json={'user': cfg['frigate_user'], 'password': cfg['frigate_password']}) as r:
            pass  # cookie jar holds frigate_token
        # Frigate snapshot of the front_doorbell camera: latest detection image
        async with s.get(frigate + '/api/front_doorbell/latest.jpg', timeout=15) as r:
            if r.status == 200:
                return await r.read()
    return None


async def snapshot_front_door(cfg=None):
    """Grab a still of the front door camera, save to SNAPSHOT_DIR, return (ok, path_or_err)."""
    cfg = cfg or _creds()
    try:
        os.makedirs(SNAPSHOT_DIR, exist_ok=True)
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
        path = os.path.join(SNAPSHOT_DIR, fname)
        if isinstance(data, str):
            data = data.encode('latin1')
        with open(path, 'wb') as f:
            f.write(data)
        log.info("snapshot saved: %s (%d bytes, %s)", path, len(data), source)
        return True, path
    except Exception as e:
        log.warning("snapshot_front_door error: %s", e)
        return False, 'snapshot error: %s' % e


async def notify_ryan(message, cfg=None, image_path=None):
    """Send a push notification to Ryan's devices via HA notify.all_devices."""
    cfg = cfg or _creds()
    payload = {'message': message, 'title': 'Doorman'}
    if image_path and os.path.exists(image_path):
        # Attach image via notify data (mobile_app supports image via data.image or the
        # message may carry the path on HA-local). Simplest robust: include the absolute
        # path in data so a companion can attach it; mobile_app needs the file served.
        payload['data'] = {'image': image_path}
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
