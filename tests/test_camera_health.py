#!/usr/bin/env python3
"""Tests for the camera health gate (audio_bridge.camera_healthy / wait_camera_healthy).

These are regression tests for the 2026-09-10 defect: the old gate returned True
whenever go2rtc's stream record had a producer with a populated remote_addr, which
stayed true while the camera's RTSP was actually unreachable ("stale producer
false positive"). The new gate keys off Frigate's real frame flow (camera_fps /
process_fps).

The tests stub the HTTP layer, so they need no network and no camera.
Run: python tests/test_camera_health.py
"""
import asyncio
import os
import sys

_here = os.path.dirname(os.path.abspath(__file__))
for _cand in (os.path.join(_here, '..', 'src'), '/app/src', os.path.join(_here, 'src')):
    if os.path.isdir(_cand):
        sys.path.insert(0, _cand)
        break
import audio_bridge as ab

PASS, FAIL = [], []


def check(name, got, want):
    if got == want:
        PASS.append(name)
        print(f"  PASS {name}: {got!r}")
    else:
        FAIL.append(name)
        print(f"  FAIL {name}: got {got!r}, want {want!r}")


class FakeResp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status = status

    async def json(self):
        return self._payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


def make_fake_session(payload, status=200):
    class FakeSession:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def get(self, url):
            return FakeResp(payload, status)

    return FakeSession


def stats_payload(camera_fps, process_fps, camera='front_doorbell'):
    return {"cameras": {camera: {"camera_fps": camera_fps, "process_fps": process_fps}}}


async def with_stub(payload, status=200, **kw):
    """Run camera_healthy against a stubbed aiohttp."""
    import aiohttp
    real = aiohttp.ClientSession
    aiohttp.ClientSession = make_fake_session(payload, status)
    try:
        return await ab.camera_healthy(**kw)
    finally:
        aiohttp.ClientSession = real


async def with_stub_seq(payloads, **kw):
    """Run camera_healthy against a sequence of responses (one per call)."""
    import aiohttp
    real = aiohttp.ClientSession
    seq = list(payloads)

    class SeqSession:
        def __init__(self, *a, **kw2):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def get(self, url):
            if len(seq) > 1:
                payload = seq.pop(0)
            else:
                payload = seq[0]          # hold the last response (do NOT fall back to
                                          # an empty payload, which would trip the
                                          # intentional fail-open path)
            return FakeResp(payload, 200)

    aiohttp.ClientSession = SeqSession
    try:
        return await ab.wait_camera_healthy(**kw)
    finally:
        aiohttp.ClientSession = real


async def main():
    print("\n== camera_healthy: frame-flow based ==")
    # The core regression: a healthy reading.
    check("healthy camera (5.0 fps)", await with_stub(stats_payload(5.0, 5.0)), True)
    # The defect: camera stopped delivering frames. Old gate said True here.
    check("camera_fps=0 (dead camera)", await with_stub(stats_payload(0.0, 0.0)), False)
    check("camera_fps=0.2 (wedge range)", await with_stub(stats_payload(0.2, 0.2)), False)
    check("process_fps alone is enough", await with_stub(stats_payload(0.0, 4.8)), True)
    check("camera_fps alone is enough", await with_stub(stats_payload(4.8, 0.0)), True)
    # Recovery transient: Frigate dips to ~0.2 for ~20-60s after a reconnect.
    check("recovery dip 0.3 is NOT ready", await with_stub(stats_payload(0.3, 0.3)), False)
    # Fail-open paths (must not stall every ring).
    check("Frigate HTTP 500 -> fail open", await with_stub({}, status=500), True)
    check("unknown camera name -> fail open", await with_stub(stats_payload(5.0, 5.0, 'other_cam')), True)
    check("empty stats -> fail open", await with_stub({}), True)
    # Unreachable Frigate -> fail open.
    import aiohttp
    real = aiohttp.ClientSession

    class Boom:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            raise OSError("connection refused")

        async def __aexit__(self, *a):
            return False

    aiohttp.ClientSession = Boom
    try:
        check("Frigate unreachable -> fail open", await ab.camera_healthy(), True)
    finally:
        aiohttp.ClientSession = real

    print("\n== wait_camera_healthy: requires CONSECUTIVE healthy samples ==")
    good = stats_payload(5.0, 5.0)
    dead = stats_payload(0.0, 0.0)
    check("two good samples -> True",
          await with_stub_seq([good, good], max_wait_s=30, poll_s=0), True)
    # One good reading must NOT open the gate (consecutive=2 default).
    check("good-then-dead -> returns False (no false positive)",
          await with_stub_seq([good, dead, dead, dead, dead, dead, dead, dead, dead, dead],
                              max_wait_s=0.01, poll_s=0), False)
    check("dead-then-recovers -> True after consecutive goods",
          await with_stub_seq([dead, good, good], max_wait_s=30, poll_s=0), True)
    check("never healthy -> False after budget",
          await with_stub_seq([dead] * 50, max_wait_s=0.01, poll_s=0), False)

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("FAILURES:", ", ".join(FAIL))
    return 1 if FAIL else 0


if __name__ == '__main__':
    raise SystemExit(asyncio.run(main()))
