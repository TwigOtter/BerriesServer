"""
tests/test_upsert_pending.py

Tests for the Phase 4 upsert + source deletion (scripts/upsert_pending.py).

This is the only place in the codebase that deletes from ChromaDB, and it runs
unattended from a nightly timer, so the ordering guarantees are pinned here:
nothing is deleted unless its summary was written first, and a summary is never
in its own delete list.
"""

import json
from unittest.mock import MagicMock, patch

import pytest

import scripts.upsert_pending as up


def _write(tmp_path, pending):
    path = tmp_path / "pending.json"
    path.write_text(json.dumps(pending), encoding="utf-8")
    return path


@pytest.fixture
def pending(tmp_path):
    return _write(tmp_path, [
        {
            "id": "summary_aaa_2026-08-12",
            "document": "a memory",
            "metadata": {"source": "summary"},
            "source_ids": ["chunk_1", "chunk_2"],
        },
        {
            "id": "summary_bbb_2026-08-12",
            "document": "another memory",
            "metadata": {"source": "summary"},
            "source_ids": ["chunk_2", "chunk_3"],
        },
    ])


def test_upserts_then_deletes_sources(pending):
    col = MagicMock()
    with patch.object(up, "get_collection", return_value=col):
        assert up.main(pending) == 0
    assert col.upsert.call_args.kwargs["ids"] == [
        "summary_aaa_2026-08-12", "summary_bbb_2026-08-12",
    ]
    # Deduplicated: chunk_2 was consolidated into both summaries.
    assert col.delete.call_args.kwargs["ids"] == ["chunk_1", "chunk_2", "chunk_3"]


def test_upsert_failure_deletes_nothing(pending):
    """The originals must survive any failure to write their replacement."""
    col = MagicMock()
    col.upsert.side_effect = RuntimeError("chroma down")
    with patch.object(up, "get_collection", return_value=col):
        assert up.main(pending) == 2
    col.delete.assert_not_called()


def test_delete_failure_is_not_fatal(pending):
    """Summaries are already written; retrying would re-spend LLM tokens."""
    col = MagicMock()
    col.delete.side_effect = RuntimeError("chroma down")
    with patch.object(up, "get_collection", return_value=col):
        assert up.main(pending) == 0


def test_never_deletes_a_summary_written_this_run(tmp_path):
    """
    A stale pending file (written before dream.py filtered self-references)
    must not be able to delete a summary this run just upserted.
    """
    path = _write(tmp_path, [{
        "id": "summary_aaa_2026-08-12",
        "document": "a memory",
        "metadata": {"source": "summary"},
        "source_ids": ["chunk_1", "summary_aaa_2026-08-12"],
    }])
    col = MagicMock()
    with patch.object(up, "get_collection", return_value=col):
        assert up.main(path) == 0
    assert col.delete.call_args.kwargs["ids"] == ["chunk_1"]


def test_no_source_ids_skips_delete_entirely(tmp_path):
    path = _write(tmp_path, [{
        "id": "summary_aaa_2026-08-12",
        "document": "a memory",
        "metadata": {"source": "summary"},
    }])
    col = MagicMock()
    with patch.object(up, "get_collection", return_value=col):
        assert up.main(path) == 0
    col.upsert.assert_called_once()
    col.delete.assert_not_called()
