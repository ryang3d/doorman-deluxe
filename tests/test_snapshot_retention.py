#!/usr/bin/env python3
"""Unit test for snapshot retention/pruning (DOORMAN_SNAPSHOT_RETENTION).

Run: .venv/bin/python tests/test_snapshot_retention.py   (from ~/doorman)
"""
import os, sys, tempfile, time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
import doorman_tools as dt


def _clear():
    for k in list(os.environ.keys()):
        if k.startswith('DOORMAN_'):
            del os.environ[k]


def _make_snaps(sdir, n):
    """Create n fake snapshot jpgs with distinct mtimes."""
    base = time.time() - n  # oldest first
    paths = []
    for i in range(n):
        p = os.path.join(sdir, 'front_door_%03d.jpg' % i)
        with open(p, 'wb') as f:
            f.write(b'\xff\xd8\xff' + b'0' * 32)
        os.utime(p, (base + i, base + i))
        paths.append(p)
    return paths


def test_retention_keeps_newest():
    _clear()
    td = tempfile.mkdtemp(prefix='doorman_retention_')
    _make_snaps(td, 6)
    os.environ['DOORMAN_SNAPSHOT_DIR'] = td
    os.environ['DOORMAN_SNAPSHOT_RETENTION'] = '3'
    dt._prune_snapshots()
    remaining = sorted(os.listdir(td))
    assert len(remaining) == 3, remaining
    # the newest (highest index) survive
    assert remaining == ['front_door_003.jpg', 'front_door_004.jpg', 'front_door_005.jpg'], remaining
    print("PASS retention keeps newest 3")


def test_retention_zero_keeps_all():
    _clear()
    td = tempfile.mkdtemp(prefix='doorman_retention0_')
    _make_snaps(td, 5)
    os.environ['DOORMAN_SNAPSHOT_DIR'] = td
    os.environ['DOORMAN_SNAPSHOT_RETENTION'] = '0'
    dt._prune_snapshots()
    assert len(os.listdir(td)) == 5, os.listdir(td)
    print("PASS retention=0 keeps all")


def test_under_limit_no_delete():
    _clear()
    td = tempfile.mkdtemp(prefix='doorman_underlimit_')
    _make_snaps(td, 2)
    os.environ['DOORMAN_SNAPSHOT_DIR'] = td
    os.environ['DOORMAN_SNAPSHOT_RETENTION'] = '10'
    dt._prune_snapshots()
    assert len(os.listdir(td)) == 2, os.listdir(td)
    print("PASS under-limit does not delete")


if __name__ == '__main__':
    test_retention_keeps_newest()
    test_retention_zero_keeps_all()
    test_under_limit_no_delete()
    print("ALL SNAPSHOT RETENTION TESTS PASS")
