"""
shared/windowing.py

Post-rerank excerpt selection: cut each kept chunk (~480 tokens) down to the
~100-token slice that is actually about the user's message, so three chunks
cost ~600 prompt tokens instead of ~1600.

How (shrink_docs):
  1. Split the chunk into segments at line boundaries — chunk text is
     "[DisplayName]: message" lines joined by "\n" (ingest_api), and a window
     cut mid-line garbles a message and embeds a fragment that represents
     nothing. A rare line longer than the window budget is sub-split by
     tokens with elision markers on the cut ends ("[Name]: ... middle ...").
  2. Build sliding windows of whole segments up to WINDOW_TOKEN_LIMIT tokens,
     advancing ~half a window per step so adjacent windows overlap.
  3. Embed every window (one batched call across all chunks) and the raw user
     message, score by L2 distance — the same lower-is-better metric as the
     CHROMA_L2_THRESHOLD / LORE_L2_THRESHOLD filters, deliberately not a
     second oppositely-signed similarity scale.
  4. Per chunk, keep the best window merged with its better-scoring immediate
     neighbour (line-range union, no extra threshold to tune) — ~150 tokens.

Chunks already at or under the merged-window size pass through untouched, and
any embedding failure fails open to the full chunks — windowing must never be
the reason retrieval breaks.
"""

import logging
import re

import numpy as np

from shared.chroma_client import embed_documents, embed_query
from shared.config import WINDOW_TOKEN_LIMIT
from shared.tokenizer import count_tokens, decode, encode

log = logging.getLogger(__name__)

# "[DisplayName]: " — the per-message prefix ingest_api writes into chunk text.
_LINE_PREFIX_RE = re.compile(r"^(\[[^\]]*\]: )")


def _split_long_line(line: str, limit: int) -> list[str]:
    """
    Token-split a single line that exceeds the window budget, marking every
    cut end with "..." so a truncated sentence can't read as a complete one.
    Each fragment repeats the "[DisplayName]: " prefix (when present) so the
    speaker survives into whichever window the fragment lands in.
    """
    m = _LINE_PREFIX_RE.match(line)
    prefix = m.group(1) if m else ""
    body = line[len(prefix):]
    budget = max(limit - count_tokens(prefix) - 4, 16)  # 4 ≈ both "..." markers
    toks = encode(body)
    pieces = [decode(toks[i : i + budget]).strip() for i in range(0, len(toks), budget)]
    return [
        f"{prefix}{'' if i == 0 else '... '}{piece}{' ...' if i < len(pieces) - 1 else ''}"
        for i, piece in enumerate(pieces)
    ]


def _split_segments(text: str, limit: int) -> list[str]:
    """Split chunk text into window-buildable segments (lines, mostly)."""
    segments: list[str] = []
    for line in text.split("\n"):
        if not line.strip():
            continue
        if count_tokens(line) <= limit:
            segments.append(line)
        else:
            segments.extend(_split_long_line(line, limit))
    return segments


def _build_windows(tok_counts: list[int], limit: int, stride: int) -> list[tuple[int, int]]:
    """
    Sliding [start, end) segment ranges: each window greedily packs whole
    segments up to `limit` tokens (always at least one), then the next window
    starts ~`stride` tokens further in, so adjacent windows overlap and a
    later merge of neighbours is a real union rather than two disjoint blobs.
    """
    windows: list[tuple[int, int]] = []
    start = 0
    n = len(tok_counts)
    while start < n:
        end = start + 1
        tokens = tok_counts[start]
        while end < n and tokens + tok_counts[end] <= limit:
            tokens += tok_counts[end]
            end += 1
        windows.append((start, end))
        if end >= n:
            break
        skipped = 0
        nxt = start
        while nxt < end - 1 and skipped < stride:
            skipped += tok_counts[nxt]
            nxt += 1
        start = max(nxt, start + 1)
    return windows


def _pick_range(windows: list[tuple[int, int]], distances: list[float]) -> tuple[int, int]:
    """
    Best window by L2 distance (argmin), merged with whichever immediate
    neighbour scores better — always exactly one when a neighbour exists.
    Deterministic and threshold-free on purpose: a window-level score cutoff
    would be a second RERANK_MIN_SCORE nobody revisits.
    """
    best = min(range(len(windows)), key=distances.__getitem__)
    neighbours = [i for i in (best - 1, best + 1) if 0 <= i < len(windows)]
    if not neighbours:
        return windows[best]
    buddy = min(neighbours, key=distances.__getitem__)
    (s1, e1), (s2, e2) = windows[best], windows[buddy]
    return (min(s1, s2), max(e1, e2))


def shrink_docs(
    query: str,
    docs: list[tuple[str, dict]],
    limit: int = WINDOW_TOKEN_LIMIT,
) -> list[tuple[str, dict]]:
    """
    Replace each oversized doc in `docs` with its most query-relevant excerpt
    (metadata untouched). Synchronous — the embedding round-trips block, so
    call via asyncio.to_thread from async code.

    The query embeds as the raw user message ('search_query:' side of nomic's
    asymmetric pair), not the rewritten search queries — those are tuned for
    vector-search recall, not for judging what should stay in the excerpt.
    """
    stride = max(limit // 2, 1)
    # (doc index, segments, windows) for every doc that actually needs cutting.
    plans: list[tuple[int, list[str], list[tuple[int, int]]]] = []
    for i, (text, _meta) in enumerate(docs):
        # Already at or under the merged-window target — inject whole.
        if count_tokens(text) <= 2 * limit:
            continue
        segments = _split_segments(text, limit)
        windows = _build_windows([count_tokens(s) for s in segments], limit, stride)
        if len(windows) > 1:
            plans.append((i, segments, windows))
    if not plans:
        return docs

    window_texts = [
        "\n".join(segments[s:e])
        for _i, segments, windows in plans
        for s, e in windows
    ]
    try:
        q_emb = embed_query(query)
        w_embs = embed_documents(window_texts)
    except Exception as e:
        log.warning("window embedding failed (%s); injecting full chunks", e)
        return docs

    out = list(docs)
    pos = 0
    for i, segments, windows in plans:
        distances = [float(np.linalg.norm(q_emb - w)) for w in w_embs[pos : pos + len(windows)]]
        pos += len(windows)
        s, e = _pick_range(windows, distances)
        out[i] = ("\n".join(segments[s:e]), docs[i][1])
        log.debug(
            "windowed doc %d: %d windows, best range [%d:%d), distances=%s",
            i, len(windows), s, e, [round(d, 3) for d in distances],
        )
    return out
