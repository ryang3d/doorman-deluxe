"""Schema grouping tests: the five 'Local engine *' sub-groups render as a
single 'Local voice engine' section in the UI.

The merge is a backend-visible change (the /api/config 'groups' array), so it
gets a real pytest rather than a browser check.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
import ui_schema as s  # noqa: E402


def test_no_local_engine_subgroups_remain():
    for g in s.groups_in_order():
        assert not g.startswith('Local engine -'), g


def test_single_merged_local_group():
    gs = s.groups_in_order()
    assert gs.count('Local voice engine') == 1


def test_merged_group_keeps_all_local_fields():
    # 7 (LLM) + 5 (TTS) + 2 (STT) + 11 (audio) + 2 (keep-warm) = 27
    n = sum(1 for f in s.FIELDS if f[2] == 'Local voice engine')
    assert n == 27, n


def test_merged_group_lands_after_voice_engine():
    gs = s.groups_in_order()
    assert 'Voice engine' in gs and 'Local voice engine' in gs
    assert gs.index('Voice engine') < gs.index('Local voice engine')


def test_total_field_count_unchanged():
    # The merge renames groups but must not drop or add fields.
    assert len(s.FIELDS) == 69, len(s.FIELDS)
