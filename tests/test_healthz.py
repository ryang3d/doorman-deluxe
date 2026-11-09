#!/usr/bin/env python3
"""Test the /healthz liveness endpoint (used by the Docker HEALTHCHECK).

Run: .venv/bin/python -m pytest tests/test_healthz.py -q
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
import pytest
import doorman_config as dc
import ui_api


@pytest.fixture
def client(aiohttp_client, tmp_path, monkeypatch):
    monkeypatch.setenv("DOORMAN_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(dc, "ENV_FILE_PATH", str(tmp_path / ".env"))
    app = ui_api.build_app()
    return aiohttp_client(app)


@pytest.mark.asyncio
async def test_healthz_ok(client):
    async with await client as c:
        r = await c.get("/healthz")
        assert r.status == 200
        d = await r.json()
        assert d["ok"] is True
        assert d["version"] == "0.1.0"
    print("PASS /healthz returns ok + version")
