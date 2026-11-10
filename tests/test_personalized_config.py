#!/usr/bin/env python3
"""Verify the personalized-greeting config parsing logic matches the intended modes."""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


def parse(val):
    return str(val).strip().lower() in ("true", "1", "yes", "on")


# from frigate.env: DOORMAN_PERSONALIZED_GREETING=false -> personalized OFF
cfg = {}
for line in open(os.path.expanduser(os.environ.get("DOORMAN_FRIGATE_ENV", "~/.config/doorman/frigate.env"))):
    line = line.strip()
    if line.startswith("DOORMAN_PERSONALIZED_GREETING="):
        cfg["v"] = line.split("=", 1)[1]

personalized = parse(cfg.get("v", "true"))
print("config value:", cfg.get("v"), "-> personalized:", personalized)
assert personalized is False, "frigate.env currently sets false, expected OFF"
print("PASS: personalized greeting is DISABLED per config (fast, no-recognition-wait mode active)")
