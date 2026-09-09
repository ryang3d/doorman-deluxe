#!/usr/bin/env python3
"""Task 3 (HA-native): verify notify_ryan attaches a doorbell frame captured by
HA camera.snapshot, served at HA /local. No container HTTP server, no SSH.

Run: .venv/bin/python tests/test_snapshot_http.py   (from ~/doorman)
"""
import asyncio, os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
import doorman_tools as dt


async def main():
    cfg = dt._creds()
    # capture a fresh doorbell frame via HA and build its /local URL (does NOT notify)
    url = await dt._ha_snapshot_url(cfg)
    assert url, "camera.snapshot failed, no URL returned"
    assert url.startswith('http://') and '/local/doorbell/' in url, url
    print("snapshot url:", url)

    # fetch it from HA and confirm it's a real JPEG
    import aiohttp
    timeout = aiohttp.ClientTimeout(total=10)
    async with aiohttp.ClientSession() as s:
        async with s.get(url, timeout=timeout) as r:
            assert r.status == 200, r.status
            body = await r.read()
            assert body[:3] == b'\xff\xd8\xff', "not a JPEG"
            print("served", len(body), "bytes, status", r.status, "(valid JPEG)")

    print("ALL HA-NATIVE SNAPSHOT TESTS PASS")


if __name__ == '__main__':
    asyncio.run(main())
