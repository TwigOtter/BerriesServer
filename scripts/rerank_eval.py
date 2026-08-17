"""
scripts/rerank_eval.py

Ad-hoc reranker eval: given a trace id, re-run the same vector search that
trace did (same rewritten queries, live ChromaDB) and show what the top 3
would have been with no reranking, next to what the trace's rerank step
actually kept. Companion to scripts/eval_retrieval.py, which scores precision
in bulk — this is for eyeballing one interaction at a time.

Note this re-queries ChromaDB live rather than reading anything logged at
request time (the trace only stores the post-rerank result, not raw
candidates/distances). If dream.py has consolidated or reindexed chunks since
the trace was recorded, results can drift from what the original request saw.

Usage:
    python scripts/rerank_eval.py <trace-id-or-prefix>
    python scripts/rerank_eval.py <trace-id-or-prefix> --full   # untruncated chunk text
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from shared.chroma_client import get_collection  # noqa: E402
from shared.config import CHROMA_L2_THRESHOLD, CHROMA_N_RESULTS, RERANK_CANDIDATES, TRACES_DIR  # noqa: E402


def _load_day(date_str: str) -> list[dict]:
    path = TRACES_DIR / f"{date_str}.jsonl"
    if not path.exists():
        return []
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return records


def _find_by_prefix(prefix: str) -> dict | None:
    """Search trace files newest-first for a trace id starting with prefix."""
    for path in sorted(TRACES_DIR.glob("*.jsonl"), reverse=True):
        for r in reversed(_load_day(path.stem)):
            if r.get("trace_id", "").startswith(prefix):
                return r
    return None


def _preview(text: str, limit: int = 150) -> str:
    text = (text or "").replace("\n", " ").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _interleave_with_distance(
    results: dict, n_results: int, l2_threshold: float
) -> list[tuple[str, dict, float]]:
    """
    Same round-robin interleaving as shared.chroma_client._interleave_results,
    but keeps the L2 distance per candidate instead of dropping it — that's
    the whole point of this script, so it's reimplemented here rather than
    imported.
    """
    per_query: list[list[tuple[str, str, dict, float]]] = [
        [
            (chunk_id, doc, meta, dist)
            for chunk_id, doc, meta, dist in zip(id_list, doc_list, meta_list, dist_list)
            if dist <= l2_threshold
        ]
        for id_list, doc_list, meta_list, dist_list in zip(
            results.get("ids", []),
            results.get("documents", []),
            results.get("metadatas", []),
            results.get("distances", []),
        )
    ]

    seen_ids: set[str] = set()
    candidates: list[tuple[str, dict, float]] = []
    for rank in range(n_results):
        for query_results in per_query:
            if len(candidates) >= n_results:
                return candidates
            if rank < len(query_results):
                chunk_id, doc, meta, dist = query_results[rank]
                if chunk_id not in seen_ids:
                    seen_ids.add(chunk_id)
                    candidates.append((doc, meta, dist))
    return candidates


def _find_match(injected_text: str, candidates: list[tuple[str, dict, float]]) -> int | None:
    """
    Find which candidate an injected (possibly window-shrunk) chunk came
    from. Windowing only ever cuts a contiguous slice of lines out of a
    chunk, so the injected text should be a substring of the original
    candidate doc (or equal to it, if windowing didn't touch it).
    Returns the candidate's index (0-based, pre-rerank rank) or None if no
    candidate matches — e.g. the source chunk was deleted/rewritten by
    dream.py since the trace was recorded.
    """
    injected_norm = injected_text.strip()
    for i, (doc, _meta, _dist) in enumerate(candidates):
        if injected_norm in doc or doc in injected_norm:
            return i
    return None


def main() -> None:
    parser = argparse.ArgumentParser(description="Compare pre- vs post-rerank retrieval for one trace.")
    parser.add_argument("trace_id", help="trace id (or prefix) to evaluate")
    parser.add_argument("--full", action="store_true", help="print untruncated chunk text")
    args = parser.parse_args()

    record = _find_by_prefix(args.trace_id)
    if record is None:
        print(f"No trace found with id prefix {args.trace_id!r}.")
        sys.exit(1)

    retrieval = record.get("data", {}).get("retrieval")
    if not retrieval or not retrieval.get("queries"):
        print(f"trace {record['trace_id']} has no retrieval data — nothing to compare.")
        sys.exit(1)

    queries = retrieval["queries"]
    n_candidates = retrieval.get("n_candidates", RERANK_CANDIDATES)
    reranked = any(s["name"].endswith(".rerank") for s in record.get("steps", []))

    print(f"trace {record['trace_id']} — {record.get('pipeline')} — {record.get('started_at')}")
    print(f"query: {record.get('meta', {}).get('query')}")
    print("rewritten queries:")
    for q in queries:
        print(f"  - {q}")
    if not reranked:
        print("\nNOTE: this trace's steps show no rerank call (RERANK_ENABLED was off, or it "
              "wasn't reached) — 'after rerank' below is really just the vector-order fallback.")
    print(
        "\nNOTE: re-querying ChromaDB live — the index may have changed since this trace was "
        "recorded (dream.py consolidation deletes/rewrites chunks nightly), so results can drift "
        "from what the original request actually saw.\n"
    )

    results = get_collection().query(
        query_texts=queries,
        n_results=n_candidates,
        include=["documents", "metadatas", "distances"],
    )
    candidates = _interleave_with_distance(results, n_candidates, CHROMA_L2_THRESHOLD)

    if not candidates:
        print("Live requery returned no candidates within the L2 threshold — can't compare.")
        sys.exit(1)

    limit = None if args.full else 150

    print(f"vector search only (no rerank) — top {CHROMA_N_RESULTS} of {len(candidates)} candidates:")
    pre_rerank = candidates[:CHROMA_N_RESULTS]
    for i, (doc, meta, dist) in enumerate(pre_rerank, 1):
        print(f"  {i}. [{meta.get('source', 'transcript')}] dist={dist:.4f}  {_preview(doc, limit)}")

    print(f"\nafter rerank — actual top {len(retrieval.get('injected', []))} injected (from trace):")
    injected = retrieval.get("injected", [])
    matched_indices: list[int | None] = []
    for i, chunk in enumerate(injected, 1):
        idx = _find_match(chunk.get("text", ""), candidates)
        matched_indices.append(idx)
        if idx is None:
            print(f"  {i}. [{chunk.get('source')}] dist=?  (source chunk not found in live requery — "
                  f"likely consolidated/reindexed since)  {_preview(chunk.get('text', ''), limit)}")
        else:
            _doc, meta, dist = candidates[idx]
            tag = "unchanged" if idx < CHROMA_N_RESULTS else f"promoted from pre-rerank rank {idx + 1}"
            print(f"  {i}. [{meta.get('source', 'transcript')}] dist={dist:.4f}  ({tag})  "
                  f"{_preview(chunk.get('text', ''), limit)}")

    kept_indices = {idx for idx in matched_indices if idx is not None}
    dropped = [c for i, c in enumerate(pre_rerank) if i not in kept_indices]
    if dropped:
        print(f"\ndropped from vector-only top {CHROMA_N_RESULTS}:")
        for doc, meta, dist in dropped:
            print(f"  - [{meta.get('source', 'transcript')}] dist={dist:.4f}  {_preview(doc, limit)}")

    unchanged = sum(1 for idx in matched_indices if idx is not None and idx < CHROMA_N_RESULTS)
    print(f"\noverlap: {unchanged}/{len(injected)} unchanged, {len(injected) - unchanged} swapped")


if __name__ == "__main__":
    main()
