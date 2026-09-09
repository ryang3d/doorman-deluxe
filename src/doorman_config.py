#!/usr/bin/env python3
"""Doorman central configuration (dockerization task 1).

Reads config from environment variables (DOORMAN_*) with the CURRENT values as
defaults, so bare-metal/venv runs and tests stay green without setting anything.
Precedence (highest first):
  1. Process environment (DOORMAN_* vars, e.g. set by docker-compose env_file)
  2. The legacy profile config files (.env and frigate.env) when present
  3. Hardcoded defaults matching today's working values

Env var naming: DOORMAN_<UPPER_KEY>. A var that is present (even empty) overrides
file/default. Callers use load() once and pass the dict around.
"""
import os

# Legacy config files this profile used (fallback so bare-metal needs no env).
PROFILE_ENV = '~/.hermes/profiles/home-admin/.env'
FRIGATE_ENV = '~/.hermes/profiles/home-admin/frigate.env'

# defaults matching the working deployment (2026-09-08)
DEFAULTS = {
    # HA
    'HASS_URL': 'http://<ha-host>:8123',
    'HASS_TOKEN': '',
    # MQTT broker carrying frigate/events
    'MQTT_HOST': '<ha-host>',
    'MQTT_PORT': 1883,
    'MQTT_USER': '',
    'MQTT_PASSWORD': '',
    # Frigate / go2rtc
    'FRIGATE_URL': 'http://<frigate-host>:5001',
    'FRIGATE_TOPIC': 'frigate/events',
    'FRONT_CAMERA': 'front_doorbell',
    # doorbell camera mic RTSP (visitor audio in)
    'CAM_MIC_RTSP': ('rtsp://admin:<doorbell-pass>@<camera-ip>:554/'
                     'cam/realmonitor?channel=1&subtype=1'),
    # Gemini Live
    'GEMINI_API_KEY': '',
    'DOORMAN_VOICE': '',          # optional prebuilt voice name
    # behaviour
    'DOORMAN_PERSONALIZED_GREETING': 'true',
    'IDLE_TIMEOUT_S': 25.0,
    'INTERACTION_MAX_S': 120.0,
    'INTERACTION_COOLDOWN_S': 20.0,
    # snapshots
    'DOORMAN_SNAPSHOT_DIR': '~/doorman/snapshots',
    'DOORMAN_SNAPSHOT_HTTP_PORT': 8120,
    'DOORMAN_SNAPSHOT_HTTP_ADVERTISE_HOST': '<doorman-host>',
    # legacy file paths (only used as fallback sources, not needed in docker)
    'PROFILE_ENV': PROFILE_ENV,
    'FRIGATE_ENV': FRIGATE_ENV,
}


def _read_kv_file(path):
    """Read key=value lines from a file into a dict (skip # and empty)."""
    out = {}
    try:
        for line in open(path):
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            if '=' in line:
                k, v = line.split('=', 1)
                out[k.strip()] = v.strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    return out


def load():
    """Return the merged config dict (env > profile files > defaults)."""
    merged = dict(DEFAULTS)
    # 2. profile files provide the legacy names (HASS_*, FRIGATE_*, GEMINI_*, MQTT_*)
    file_cfg = {}
    file_cfg.update(_read_kv_file(PROFILE_ENV))
    file_cfg.update(_read_kv_file(FRIGATE_ENV))
    # map legacy file keys onto our internal keys where the name matches or maps
    legacy_map = {
        'HASS_URL': 'HASS_URL', 'HASS_TOKEN': 'HASS_TOKEN',
        'MQTT_HOST': 'MQTT_HOST', 'MQTT_PORT': 'MQTT_PORT',
        'MQTT_USER': 'MQTT_USER', 'MQTT_PASSWORD': 'MQTT_PASSWORD',
        'FRIGATE_URL': 'FRIGATE_URL', 'FRIGATE_TOPIC': 'FRIGATE_TOPIC',
        'FRONT_CAMERA': 'FRONT_CAMERA', 'CAM_MIC_RTSP': 'CAM_MIC_RTSP',
        'GEMINI_API_KEY': 'GEMINI_API_KEY', 'DOORMAN_VOICE': 'DOORMAN_VOICE',
        'DOORMAN_PERSONALIZED_GREETING': 'DOORMAN_PERSONALIZED_GREETING',
        'IDLE_TIMEOUT_S': 'IDLE_TIMEOUT_S', 'INTERACTION_MAX_S': 'INTERACTION_MAX_S',
        'INTERACTION_COOLDOWN_S': 'INTERACTION_COOLDOWN_S',
    }
    for src_key, dst_key in legacy_map.items():
        if src_key in file_cfg and file_cfg[src_key] not in (None, ''):
            merged[dst_key] = file_cfg[src_key]

    # 1. env overrides (highest)
    for key in list(DEFAULTS.keys()):
        env_key = key if key.startswith('DOORMAN_') else 'DOORMAN_' + key
        # also accept exact legacy env names for the legacy keys
        candidates = [env_key, key]
        for c in candidates:
            if c in os.environ:
                merged[key] = os.environ[c]
                break

    # normalise numerics / booleans
    for numk in ('MQTT_PORT', 'IDLE_TIMEOUT_S', 'INTERACTION_MAX_S', 'INTERACTION_COOLDOWN_S',
                 'DOORMAN_SNAPSHOT_HTTP_PORT'):
        try:
            merged[numk] = float(merged[numk]) if numk != 'MQTT_PORT' else int(float(merged[numk]))
            if numk in ('MQTT_PORT', 'DOORMAN_SNAPSHOT_HTTP_PORT'):
                merged[numk] = int(merged[numk])
        except (TypeError, ValueError):
            pass
    merged['DOORMAN_PERSONALIZED_GREETING'] = str(
        merged['DOORMAN_PERSONALIZED_GREETING']).strip().lower() in ('true', '1', 'yes', 'on')
    return merged
