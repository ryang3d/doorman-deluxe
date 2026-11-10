#!/usr/bin/env python3
"""Doorman health check (Phase 4 observability).

Pings the key dependencies Doorman needs to work and reports pass/fail each:
  1. Frigate / go2rtc API - provides door events + camera
  2. MQTT broker - carries frigate/events trigger
  3. Home Assistant API - notify + snapshot publishing
  4. Camera RTSP - doorbell mic audio
  5. The doorman.service itself (systemd user service)

All URLs/credentials come from the normal config chain (env vars > profile
files > defaults), so this script works anywhere Doorman works:
  export PATH="$HOME/doorman/src:$PATH"  # or run with PYTHONPATH=$HOME/doorman/src
  python3 deploy/health_check.py

Exit 0 if the critical path is healthy, 1 if not. Prints one line per dependency.
"""

import asyncio
import os
import socket
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
import doorman_config


def _cfg():
    return doorman_config.load()


async def check_frigate():
    cfg = _cfg()
    frigate = cfg.get("FRIGATE_URL")
    import aiohttp

    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(frigate + "/api/version", timeout=8) as r:
                ok = r.status == 200
                return ok, f"frigate api {frigate} -> {r.status}"
    except Exception as e:
        return False, f"frigate api unreachable: {e}"


async def check_mqtt():
    cfg = _cfg()
    import paho.mqtt.client as mqtt
    import threading

    res = {"ok": False, "reason": "timeout"}
    done = threading.Event()

    def on_connect(c, u, flags, rc, *a):
        res["ok"] = rc == 0
        res["reason"] = f"rc={rc}"
        done.set()

    c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    c.username_pw_set(cfg.get("MQTT_USER") or "", cfg.get("MQTT_PASSWORD") or "")
    c.on_connect = on_connect
    try:
        c.connect(str(cfg.get("MQTT_HOST")), int(cfg.get("MQTT_PORT", 1883)), 10)
    except Exception as e:
        return False, f"mqtt connect fail: {e}"
    c.loop_start()
    done.wait(8)
    c.loop_stop()
    return res["ok"], f"mqtt {cfg.get('MQTT_HOST')}:{cfg.get('MQTT_PORT')} -> {res['reason']}"


async def check_hass():
    cfg = _cfg()
    u, t = cfg.get("HASS_URL"), cfg.get("HASS_TOKEN")
    import aiohttp

    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(u + "/api/", headers={"Authorization": "Bearer " + t}, timeout=8) as r:
                return r.status == 200, f"hass api {u} -> {r.status}"
    except Exception as e:
        return False, f"hass unreachable: {e}"


async def check_camera_rtsp():
    # probe the camera RTSP port from CAM_MIC_RTSP
    hostport = str(_cfg().get("CAM_MIC_RTSP")).split("://", 1)[-1]
    host = hostport.split(":", 1)[0]
    port = 554
    if "://" in str(_cfg().get("CAM_MIC_RTSP")):
        try:
            port = int(hostport.split(":", 1)[1].split("/", 1)[0])
        except (IndexError, ValueError):
            pass
    try:
        s = socket.create_connection((host, port), timeout=6)
        s.close()
        return True, f"camera rtsp {host}:{port} open"
    except Exception as e:
        return False, f"camera rtsp unreachable: {e}"


def check_service():
    try:
        r = subprocess.run(
            ["systemctl", "--user", "is-active", "doorman.service"], capture_output=True, text=True, timeout=10
        )
        return r.stdout.strip() == "active", f"doorman.service -> {r.stdout.strip()}"
    except Exception as e:
        return False, f"service check err: {e}"


async def main():
    checks = [
        ("frigate", check_frigate()),
        ("mqtt", check_mqtt()),
        ("hass", check_hass()),
        ("camera_rtsp", check_camera_rtsp()),
        ("service", asyncio.to_thread(check_service)),
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
