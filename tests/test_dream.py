"""
tests/test_dream.py

Tests for dream.py's unarchived-day discovery (the catch-up scan).
"""

from unittest.mock import patch

import scripts.dream as dream


def _touch(directory, *names):
    for name in names:
        (directory / name).write_text("{}", encoding="utf-8")


def test_collects_only_days_before_today(tmp_path):
    _touch(
        tmp_path,
        "2026-06-09.json",
        "2026-06-10.json",
        "2026-06-11.json",          # today — still being written
        "2026-06-09_retrievals.json",
        "2026-06-10_retrievals.json",
    )
    with patch.object(dream, "_INTERACTIONS_DIR", tmp_path):
        interactions, retrievals = dream._unarchived_dates("2026-06-11")
    assert interactions == ["2026-06-09", "2026-06-10"]
    assert retrievals == ["2026-06-09", "2026-06-10"]


def test_ignores_non_log_files_and_subdirs(tmp_path):
    (tmp_path / "archive").mkdir()
    (tmp_path / "pending").mkdir()
    _touch(
        tmp_path,
        "2026-06-10.json",
        "2026-06-10.json.lock",
        "2026-06-10_retrievals.tmp",
        "notes.json",
    )
    _touch(tmp_path / "archive", "2026-06-01.json")
    with patch.object(dream, "_INTERACTIONS_DIR", tmp_path):
        interactions, retrievals = dream._unarchived_dates("2026-06-11")
    assert interactions == ["2026-06-10"]
    assert retrievals == []


def test_missing_directory_returns_empty(tmp_path):
    with patch.object(dream, "_INTERACTIONS_DIR", tmp_path / "does-not-exist"):
        assert dream._unarchived_dates("2026-06-11") == ([], [])


# ── _strip_profile_header ──────────────────────────────────────────────────

def test_strip_profile_header_removes_leaked_fields():
    blurb = "Name: Twig\nSpecies: otter\nPronouns: he/him\n\nTwig is an otter with rotating hyperfixations."
    assert dream._strip_profile_header(blurb) == "Twig is an otter with rotating hyperfixations."


def test_strip_profile_header_leaves_clean_blurbs_alone():
    blurb = "Twig is an otter. His species: otter, is important to him."
    assert dream._strip_profile_header(blurb) == blurb
