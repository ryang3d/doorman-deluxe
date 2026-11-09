"""Pytest collection config for Doorman Deluxe.

`tests/` contains two kinds of files:
  * pytest unit tests - collected and run by CI (the test_*.py files with test
    functions in them).
  * manual diagnostic scripts - also named test_*.py for historical reasons.
    They are run ad-hoc against a live deployment (they sys.exit() at import
    time, so letting pytest collect them crashes the whole session).

Diagnostic scripts live under tests/ but are listed in collect_ignore below.
New diagnostic scripts should either go in this list or NOT be prefixed with
test_ (e.g. diag_*.py, repro_*.py, echo_*.py are already ignored by pytest's
default collection pattern).
"""

collect_ignore = [
    "tests/test_camera_health.py",
    "tests/test_echogate.py",
    "tests/test_idle_clock.py",
    "tests/test_ignored_faces.py",
    "tests/test_mqtt_conn.py",
    "tests/test_notify_direct.py",
    "tests/test_notify_img.py",
    "tests/test_notify.py",
    "tests/test_personalized_config.py",
    "tests/test_recognition_trigger.py",
    "tests/test_resample.py",
    "tests/test_resampler_cmp.py",
    "tests/test_snapshot_http.py",
    "tests/test_snapshot.py",
    "tests/test_track2.py",
    "tests/test_track.py",
]
