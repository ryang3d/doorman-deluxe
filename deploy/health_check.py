#!/usr/bin/env python3
"""Doorman health check (Phase 4 observability).

Pings the key dependencies Doorman needs to work and reports pass/fail each:
  1. Frigate / go2rtc API (<frigate-host>:5001) - provides door events + camera
  2. MQTT broker (<ha-host>:1883) - carries frigate/events trigger
  3. Home Assistant API (<ha-host>:8123) - notify + snapshot publishing
  4. Camera RTSP (<camera-ip>:554) - doorbell mic audio
  5. The doorman.service itself (systemd user service)

Exit 0 if the critical path is healthy, 1 if not. Prints one line per dependency.
"""
import asyncio, sys, os, json, subprocess

def getenv(path, key):
    for line in open(os.path.expanduser(path)):
        line = line.strip()
        if line.startswith(key + '='):
            return line.split('=', 1)[1].strip().strip('"').strip("'")
    return None

async def check_frigate():
    frigate = getenv('~/.hermes/profiles/home-admin/frigate.env', 'FRIGATE_URL')
    import aiohttp
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(frigate + '/api/version', timeout=8) as r:
                ok = r.status == 200
                return ok, f"frigate api {frigate} -> {r.status}"
    except Exception as e:
        return False, f"frigate api unreachable: {e}"

async def check_mqtt():
    user = getenv('~/.hermes/profiles/home-admin/.env', 'MQTT_USER')
    pw = getenv('~/.hermes/profiles/home-admin/.env', 'MQTT_PASSWORD')
    import paho.mqtt.client as mqtt
    import threading, time
    res = {'ok': False, 'reason': 'timeout'}
    done = threading.Event()
    def on_connect(c, u, flags, rc, *a):
        res['ok'] = rc == 0
        res['reason'] = f"rc={rc}"
        done.set()
    c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    c.username_pw_set(user, pw)
    c.on_connect = on_connect
    try:
        c.connect('<ha-host>', 1883, 10)
    except Exception as e:
        return False, f"mqtt connect fail: {e}"
    c.loop_start()
    done.wait(8)
    c.loop_stop()
    return res['ok'], f"mqtt <ha-host>:1883 -> {res['reason']}"

async def check_hass():
    u = getenv('~/.hermes/profiles/home-admin/.env', 'HASS_URL')
    t = getenv('~/.hermes/profiles/home-admin/.env', 'HASS_TOKEN')
    import aiohttp
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(u + '/api/', headers={'Authorization': 'Bearer ' + t}, timeout=8) as r:
                return r.status == 200, f"hass api {u} -> {r.status}"
    except Exception as e:
        return False, f"hass unreachable: {e}"

async def check_camera_rtsp():
    # probe the AD410 RTSP port
    import socket
    try:
        s = socket.create_connection(('<camera-ip>', 554), timeout=6)
        s.close()
        return True, "camera rtsp <camera-ip>:554 open"
    except Exception as e:
        return False, f"camera rtsp unreachable: {e}"

def check_service():
    try:
        r = subprocess.run(['systemctl', '--user', 'is-active', 'doorman.service'],
                           capture_output=True, text=True, timeout=10)
        return r.stdout.strip() == 'active', f"doorman.service -> {r.stdout.strip()}"
    except Exception as e:
        return False, f"service check err: {e}"

async def main():
    checks = [
        ('frigate', check_frigate()),
        ('mqtt', check_mqtt()),
        ('hass', check_hass()),
        ('camera_rtsp', check_camera_rtsp()),
        ('service', asyncio.to_thread(check_service)),
    ]
    allok = True
    for name, coro in checks:
        try:
            ok, msg = await coro
        except Exception as e:
            ok, msg = False, f"check error: {e}"
        print(f"[{'OK ' if ok else 'FAIL'}] {name}: {msg}")
        allok = allok and ok
    print("HEALTH:", "ALL OK" if allok else "ISSUES FOUND")
    return 0 if allok else 1

sys.exit(asyncio.run(main()))
