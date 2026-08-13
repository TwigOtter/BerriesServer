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


# ── Consolidation budgeting ──────────────────────────────────────────────────

def test_fit_passages_drops_chunks_that_do_not_fit():
    hits = [("a", "word " * 100, {}), ("b", "word " * 100, {}), ("c", "word " * 100, {})]
    kept = dream._fit_passages(hits, budget=260)
    # ~101 tokens + 16 overhead each: two fit in 260, the third does not.
    assert [cid for cid, _, _ in kept] == ["a", "b"]


def test_fit_passages_always_keeps_one_chunk():
    """A single oversized chunk still gets consolidated rather than skipped."""
    hits = [("a", "word " * 5000, {})]
    assert len(dream._fit_passages(hits, budget=10)) == 1


def test_passage_budget_leaves_room_for_completion():
    with patch.object(dream, "LLM_BACKEND", "vllm"), \
         patch.object(dream, "VLLM_CONTEXT_TOKENS", 4096):
        budget = dream._passage_budget("word " * 1000, "word " * 100)
        # 4096 - ~1001 - ~101 - 500 completion - 128 margin
        assert 2300 < budget < 2400


def test_passage_budget_unbounded_off_vllm():
    with patch.object(dream, "LLM_BACKEND", "anthropic"):
        assert dream._passage_budget("word " * 5000, "") == 100_000


def test_chunk_date_prefers_metadata_then_id():
    assert dream._chunk_date("x", {"stream_date": "2026-07-26"}) == "2026-07-26"
    assert dream._chunk_date("x", {"start_time": "2026-08-12T02:04:50+00:00"}) == "2026-08-12"
    assert dream._chunk_date("discord_2026-08-12T02-04-50_f75384", {}) == "2026-08-12"
    assert dream._chunk_date("nodate", {}) == "an unknown date"


# ── Speaker resolution (pronouns in consolidation) ───────────────────────────

def test_normalise_name_reconciles_chunk_prefix_with_db_row():
    # Chunk text carries the Discord display name + discriminator; the row
    # holds the username. Both must fold to the same key.
    assert dream._normalise_name("Teeka#8081") == dream._normalise_name("_teeka")
    assert dream._normalise_name("Missoula_mac") == "missoulamac"
    assert dream._normalise_name("  TwigOtter  ") == "twigotter"


def test_speaker_index_refuses_ambiguous_matches():
    """Two different users folding to one key must resolve to nothing."""
    users = [
        {"id": 1, "t_login": "night_owl", "pronouns": "she/her"},
        {"id": 2, "d_username": "nightowl", "pronouns": "he/him"},
    ]
    with patch.object(dream, "get_all_users", return_value=users):
        index = dream._build_speaker_index()
    assert index["nightowl"] is None


def test_speaker_profiles_defaults_missing_pronouns_to_they_them():
    users = [{"id": 1, "t_login": "someone", "about": "likes rocks"}]
    with patch.object(dream, "get_all_users", return_value=users):
        index = dream._build_speaker_index()
    block = dream._speaker_profiles([("c1", "[someone]: hi", {})], index)
    assert "Pronouns: they/them" in block


def test_speaker_profiles_uses_stored_pronouns_when_set():
    users = [{"id": 1, "d_username": "_teeka", "pronouns": "he/him", "about": "red panda"}]
    with patch.object(dream, "get_all_users", return_value=users):
        index = dream._build_speaker_index()
    block = dream._speaker_profiles([("c1", "[Teeka#8081]: morning", {})], index)
    assert "Pronouns: he/him" in block
    assert "Name: Teeka" in block  # display name, not the normalised key


def test_speaker_profiles_omits_berries_and_unknown_users():
    users = [{"id": 1, "t_login": "someone", "pronouns": "he/him", "about": "x"}]
    with patch.object(dream, "get_all_users", return_value=users):
        index = dream._build_speaker_index()
    block = dream._speaker_profiles(
        [("c1", "[BerriesTheDemon]: boo\n[nobody_here]: hi\n[someone]: hey", {})], index
    )
    assert block.count("USER PROFILE") == 1


def test_speaker_profiles_drops_live_local_time():
    """Wall-clock time of the dream run is meaningless inside a past memory."""
    users = [{"id": 1, "t_login": "someone", "timezone": "America/Chicago", "about": "x"}]
    with patch.object(dream, "get_all_users", return_value=users):
        index = dream._build_speaker_index()
    assert "Local time" not in dream._speaker_profiles([("c1", "[someone]: hi", {})], index)
