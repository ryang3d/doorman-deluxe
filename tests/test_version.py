#!/usr/bin/env python3
"""Unit tests for version plumbing: src/version.py + `doorman.py --version`.

Run: .venv/bin/python -m pytest tests/test_version.py -q
"""

import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
import version as ver


def test_version_is_semver():
    assert re.fullmatch(r"\d+\.\d+\.\d+", ver.__version__), ver.__version__
    print("PASS version is semver")


def test_cli_reports_version():
    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    r = subprocess.run(
        [sys.executable, os.path.join(repo_root, "src", "doorman.py"), "--version"],
        capture_output=True,
        text=True,
        cwd=repo_root,
        timeout=120,
    )
    assert r.returncode == 0, r.stderr
    assert ver.__version__ in r.stdout, r.stdout
    print("PASS --version flag reports", ver.__version__)


if __name__ == "__main__":
    test_version_is_semver()
    test_cli_reports_version()
    print("ALL VERSION TESTS PASS")
