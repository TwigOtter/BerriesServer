"""
tests/test_retrieval.py

Unit tests for the retrieval stage — reranker scoring/abstain behaviour and
the retrieve_context pipeline wiring. All LLM and ChromaDB calls are mocked.
"""

import pytest
from unittest.mock import AsyncMock, patch

from shared.retrieval import rerank_chunks, retrieve_context


def _docs(n: int) -> list[tuple[str, dict]]:
    return [(f"chunk text {i}", {"source": "twitch", "i": i}) for i in range(n)]


class TestRerankChunks:
    async def test_keeps_top_k_by_score(self):
        docs = _docs(4)
        with patch(
            "shared.retrieval.get_completion",
            new=AsyncMock(return_value='{"0": 2, "1": 9, "2": 7, "3": 10}'),
        ):
            kept = await rerank_chunks("query", docs, top_k=2, min_score=5)
        assert [meta["i"] for _, meta in kept] == [3, 1]

    async def test_drops_below_min_score(self):
        docs = _docs(3)
        with patch(
            "shared.retrieval.get_completion",
            new=AsyncMock(return_value='{"0": 8, "1": 3, "2": 1}'),
        ):
            kept = await rerank_chunks("query", docs, top_k=3, min_score=5)
        assert [meta["i"] for _, meta in kept] == [0]

    async def test_abstains_when_nothing_relevant(self):
        docs = _docs(3)
        with patch(
            "shared.retrieval.get_completion",
            new=AsyncMock(return_value='{"0": 1, "1": 0, "2": 2}'),
        ):
            kept = await rerank_chunks("query", docs, top_k=3, min_score=5)
        assert kept == []

    async def test_empty_candidates_short_circuit(self):
        mock_llm = AsyncMock()
        with patch("shared.retrieval.get_completion", new=mock_llm):
            kept = await rerank_chunks("query", [], top_k=4)
        assert kept == []
        mock_llm.assert_not_awaited()

    async def test_fails_open_on_bad_json(self):
        docs = _docs(3)
        with patch(
            "shared.retrieval.get_completion",
            new=AsyncMock(return_value="sorry, I cannot score these"),
        ):
            kept = await rerank_chunks("query", docs, top_k=2, min_score=5)
        assert kept == docs[:2]

    async def test_fails_open_on_llm_error(self):
        docs = _docs(3)
        with patch(
            "shared.retrieval.get_completion",
            new=AsyncMock(side_effect=RuntimeError("api down")),
        ):
            kept = await rerank_chunks("query", docs, top_k=2, min_score=5)
        assert kept == docs[:2]

    async def test_tolerates_json_wrapped_in_prose(self):
        docs = _docs(2)
        with patch(
            "shared.retrieval.get_completion",
            new=AsyncMock(return_value='Here are the scores: {"0": 9, "1": 1}'),
        ):
            kept = await rerank_chunks("query", docs, top_k=2, min_score=5)
        assert [meta["i"] for _, meta in kept] == [0]


class TestRetrieveContext:
    async def test_logs_only_raw_chunks(self):
        """Summary and lore chunks must not reach the retrieval log."""
        docs = [
            ("raw chunk", {"source": "twitch"}),
            ("a summary", {"source": "summary"}),
            ("lore entry", {"source": "lore"}),
        ]
        with (
            patch("shared.retrieval.rewrite_queries", new=AsyncMock(return_value=["q"])),
            patch("shared.retrieval.query_chroma_multi", return_value=docs),
            patch("shared.retrieval.rerank_chunks", new=AsyncMock(return_value=docs)),
            patch("shared.retrieval.RERANK_ENABLED", True),
            patch("shared.retrieval.log_retrieval") as mock_log,
        ):
            result, queries = await retrieve_context("hello", "", "viewer")
        assert result == docs
        mock_log.assert_called_once_with(query="hello", chunks=["raw chunk"])

    async def test_returns_empty_on_failure(self):
        with patch(
            "shared.retrieval.rewrite_queries",
            new=AsyncMock(side_effect=RuntimeError("boom")),
        ):
            docs, queries = await retrieve_context("hello", "", "viewer")
        assert docs == []
        assert queries == []

    async def test_rerank_disabled_truncates_vector_order(self):
        docs = _docs(6)
        with (
            patch("shared.retrieval.rewrite_queries", new=AsyncMock(return_value=["q"])),
            patch("shared.retrieval.query_chroma_multi", return_value=docs),
            patch("shared.retrieval.RERANK_ENABLED", False),
            patch("shared.retrieval.CHROMA_N_RESULTS", 4),
            patch("shared.retrieval.log_retrieval"),
        ):
            result, _ = await retrieve_context("hello", "", "viewer")
        assert result == docs[:4]
