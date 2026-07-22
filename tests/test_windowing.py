"""
tests/test_windowing.py

Unit tests for post-rerank window selection — segment splitting (line
boundaries, oversized-line elision), sliding-window construction, the
best-plus-better-neighbour merge, and shrink_docs wiring with embeddings
mocked. No embed service or ChromaDB required.
"""

import numpy as np
import pytest
from unittest.mock import patch

from shared.tokenizer import count_tokens
from shared.windowing import (
    _build_windows,
    _pick_range,
    _split_segments,
    shrink_docs,
)


def _line(name: str, words: int) -> str:
    return f"[{name}]: " + " ".join(["word"] * words)


class TestSplitSegments:
    def test_short_lines_pass_through_whole(self):
        text = "\n".join([_line("Twig", 5), _line("Viewer", 8)])
        assert _split_segments(text, limit=100) == text.split("\n")

    def test_blank_lines_dropped(self):
        text = f"{_line('Twig', 5)}\n\n{_line('Viewer', 5)}"
        assert len(_split_segments(text, limit=100)) == 2

    def test_oversized_line_split_with_elision_both_ways(self):
        long = _line("Rambler", 300)
        segments = _split_segments(long, limit=100)
        assert len(segments) > 1
        # Every fragment keeps the speaker prefix and fits the budget.
        assert all(s.startswith("[Rambler]: ") for s in segments)
        assert all(count_tokens(s) <= 100 for s in segments)
        # First: trailing marker only; middle: both; last: leading only.
        assert segments[0].endswith("...") and "]: ..." not in segments[0]
        assert segments[-1].startswith("[Rambler]: ...")
        assert not segments[-1].endswith("...")
        for mid in segments[1:-1]:
            assert mid.startswith("[Rambler]: ...") and mid.endswith("...")

    def test_oversized_line_without_prefix_still_marked(self):
        prose = " ".join(["word"] * 300)
        segments = _split_segments(prose, limit=100)
        assert len(segments) > 1
        assert segments[0].endswith("...")
        assert segments[-1].startswith("...")


class TestBuildWindows:
    def test_single_window_when_everything_fits(self):
        assert _build_windows([10, 10, 10], limit=100, stride=50) == [(0, 3)]

    def test_windows_cover_all_segments_and_overlap(self):
        counts = [30] * 10  # 300 tokens total
        windows = _build_windows(counts, limit=100, stride=50)
        assert windows[0][0] == 0
        assert windows[-1][1] == len(counts)
        for (s1, e1), (s2, e2) in zip(windows, windows[1:]):
            assert s1 < s2 <= e1  # forward progress, contiguous or overlapping

    def test_windows_respect_token_limit(self):
        counts = [40, 40, 40, 40]
        for s, e in _build_windows(counts, limit=100, stride=50):
            assert sum(counts[s:e]) <= 100

    def test_single_oversized_segment_gets_own_window(self):
        assert _build_windows([250], limit=100, stride=50) == [(0, 1)]


class TestPickRange:
    def test_merges_better_neighbour(self):
        # Twig's example: argmin is index 2; left neighbour (0.7) beats right (0.9).
        windows = [(0, 2), (1, 3), (2, 4), (3, 5)]
        assert _pick_range(windows, [1.2, 0.7, 0.6, 0.9]) == (1, 4)

    def test_best_at_start_only_right_neighbour(self):
        windows = [(0, 2), (1, 3), (2, 4)]
        assert _pick_range(windows, [0.4, 0.9, 1.1]) == (0, 3)

    def test_best_at_end_only_left_neighbour(self):
        windows = [(0, 2), (1, 3), (2, 4)]
        assert _pick_range(windows, [1.1, 0.9, 0.4]) == (1, 4)

    def test_single_window_no_merge(self):
        assert _pick_range([(0, 2)], [0.5]) == (0, 2)


class TestShrinkDocs:
    def test_small_docs_pass_through_without_embedding(self):
        docs = [(_line("Twig", 20), {"source": "twitch"})]
        with (
            patch("shared.windowing.embed_query") as eq,
            patch("shared.windowing.embed_documents") as ed,
        ):
            out = shrink_docs("query", docs, limit=100)
        assert out == docs
        eq.assert_not_called()
        ed.assert_not_called()

    def test_shrinks_to_best_window_plus_neighbour(self):
        # 12 lines x ~30 tokens = ~360 tokens; the marker line should win.
        lines = [_line(f"User{i}", 25) for i in range(12)]
        lines[6] = "[Twig]: the secret mushroom fact " + " ".join(["word"] * 20)
        doc_text = "\n".join(lines)

        def fake_embed_documents(texts):
            # Windows containing the marker embed at the query point.
            return [
                np.zeros(4) if "mushroom" in t else np.ones(4)
                for t in texts
            ]

        with (
            patch("shared.windowing.embed_query", return_value=np.zeros(4)),
            patch("shared.windowing.embed_documents", side_effect=fake_embed_documents),
        ):
            out = shrink_docs("query", [(doc_text, {"source": "twitch"})], limit=100)

        (text, meta), = out
        assert meta == {"source": "twitch"}
        assert "mushroom" in text
        assert count_tokens(text) < count_tokens(doc_text)
        # Excerpt is a contiguous run of original lines, not a re-paste.
        assert text in doc_text

    def test_metadata_and_order_preserved_across_mixed_docs(self):
        big = "\n".join(_line(f"User{i}", 25) for i in range(12))
        small = _line("Twig", 10)
        docs = [(big, {"i": 0}), (small, {"i": 1}), (big, {"i": 2})]
        with (
            patch("shared.windowing.embed_query", return_value=np.zeros(4)),
            patch(
                "shared.windowing.embed_documents",
                side_effect=lambda texts: [np.ones(4) for _ in texts],
            ),
        ):
            out = shrink_docs("query", docs, limit=100)
        assert [meta["i"] for _, meta in out] == [0, 1, 2]
        assert out[1] == (small, {"i": 1})
        assert count_tokens(out[0][0]) < count_tokens(big)

    def test_fails_open_when_embedding_breaks(self):
        big = "\n".join(_line(f"User{i}", 25) for i in range(12))
        docs = [(big, {"source": "twitch"})]
        with patch(
            "shared.windowing.embed_query",
            side_effect=RuntimeError("embed service down"),
        ):
            out = shrink_docs("query", docs, limit=100)
        assert out == docs

    def test_batches_all_windows_in_one_embed_call(self):
        big = "\n".join(_line(f"User{i}", 25) for i in range(12))
        docs = [(big, {"i": 0}), (big, {"i": 1})]
        with (
            patch("shared.windowing.embed_query", return_value=np.zeros(4)),
            patch(
                "shared.windowing.embed_documents",
                side_effect=lambda texts: [np.ones(4) for _ in texts],
            ) as ed,
        ):
            shrink_docs("query", docs, limit=100)
        assert ed.call_count == 1


class TestRetrievalWiring:
    async def test_retrieve_context_windows_after_rerank(self):
        from shared.retrieval import retrieve_context

        docs = [("chunk text", {"source": "twitch"})]
        shrunk = [("chunk", {"source": "twitch"})]
        from unittest.mock import AsyncMock

        with (
            patch("shared.retrieval.rewrite_queries", new=AsyncMock(return_value=["q"])),
            patch("shared.retrieval.query_chroma_multi", return_value=docs),
            patch("shared.retrieval.rerank_chunks", new=AsyncMock(return_value=docs)),
            patch("shared.retrieval.RERANK_ENABLED", True),
            patch("shared.retrieval.WINDOW_ENABLED", True),
            patch("shared.retrieval.shrink_docs", return_value=shrunk) as mock_shrink,
            patch("shared.retrieval.log_retrieval") as mock_log,
        ):
            result, _ = await retrieve_context("hello", "", "viewer")
        assert result == shrunk
        mock_shrink.assert_called_once_with("hello", docs)
        # The retrieval log records the injected excerpt, not the full chunk.
        mock_log.assert_called_once_with(query="hello", chunks=["chunk"])

    async def test_window_disabled_leaves_docs_alone(self):
        from shared.retrieval import retrieve_context
        from unittest.mock import AsyncMock

        docs = [("chunk text", {"source": "twitch"})]
        with (
            patch("shared.retrieval.rewrite_queries", new=AsyncMock(return_value=["q"])),
            patch("shared.retrieval.query_chroma_multi", return_value=docs),
            patch("shared.retrieval.rerank_chunks", new=AsyncMock(return_value=docs)),
            patch("shared.retrieval.RERANK_ENABLED", True),
            patch("shared.retrieval.WINDOW_ENABLED", False),
            patch("shared.retrieval.shrink_docs") as mock_shrink,
            patch("shared.retrieval.log_retrieval"),
        ):
            result, _ = await retrieve_context("hello", "", "viewer")
        assert result == docs
        mock_shrink.assert_not_called()
