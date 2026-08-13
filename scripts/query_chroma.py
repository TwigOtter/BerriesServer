"""
scripts/query_chroma.py

Interactive CLI tool for testing ChromaDB queries and inspecting L2 distances.

Usage (from repo root, with venv active):
    python scripts/query_chroma.py "what did twig say about disc golf"
    python scripts/query_chroma.py "disc golf" "dark souls" "movie night"
    python scripts/query_chroma.py --n 6 "some query"

    # Show what shared/windowing.py would cut each chunk down to, and how the
    # excerpt re-scores against the query (one or more token limits):
    python scripts/query_chroma.py --window 100 "some query"
    python scripts/query_chroma.py --window 50,100,200 --brief "some query"
"""

import argparse
import sys
import textwrap

# Allow imports from repo root
sys.path.insert(0, __import__("pathlib").Path(__file__).resolve().parent.parent.__str__())

import numpy as np

from shared.chroma_client import embed_documents, embed_query, get_collection
from shared.config import CHROMA_N_RESULTS, WINDOW_TOKEN_LIMIT
from shared.tokenizer import count_tokens
from shared.windowing import shrink_docs


def cosine_sim(dist: float) -> float:
    """
    Convert a Chroma distance to cosine similarity.

    The collection's space is "l2", for which Chroma returns the *squared*
    euclidean distance, and our embeddings are unit-norm: ||a-b||² = 2 - 2cos.
    `dist` is therefore Chroma's raw number — the same scale
    CHROMA_L2_THRESHOLD / LORE_L2_THRESHOLD are compared against.
    """
    return 1.0 - dist / 2.0


def label(dist: float) -> str:
    sim = cosine_sim(dist)
    if sim >= 0.75:
        return "strong"
    if sim >= 0.5:
        return "moderate"
    if sim >= 0.25:
        return "weak"
    return "noise"


def indent(text: str, prefix: str = "       ") -> str:
    """Wrap at 68 chars, preserving the chunk's own line breaks."""
    return "\n".join(
        textwrap.fill(line, width=68, initial_indent=prefix, subsequent_indent=prefix + "  ")
        if line.strip() else prefix
        for line in text.strip().split("\n")
    )


def window_pass(query: str, docs: list[tuple[str, dict]], limit: int) -> list[tuple[str, float]]:
    """
    Run the real windowing stage at `limit` tokens and re-score each excerpt.

    Excerpts are embedded document-side and the query query-side — the same
    asymmetric pair Chroma used for the original distances — and the norm is
    squared to land on Chroma's "l2" scale, so the two numbers are directly
    comparable. (windowing.py itself ranks on the unsquared norm; squaring is
    monotonic, so it picks the same window either way.)
    """
    shrunk = shrink_docs(query, docs, limit=limit)
    texts = [text for text, _meta in shrunk]
    q_emb = embed_query(query)
    embs = embed_documents(texts)
    return [(t, float(np.linalg.norm(q_emb - e)) ** 2) for t, e in zip(texts, embs)]


def run(queries: list[str], n_results: int, window_limits: list[int], brief: bool) -> None:
    print(f"\nQuerying ChromaDB  (n_results={n_results} per query)")
    if window_limits:
        print(f"Windowing at {', '.join(str(l) for l in window_limits)} tokens "
              f"(merged excerpt ≈ 2× the limit; chunks at or under 2× pass through whole)")
    print()
    collection = get_collection()
    results = collection.query(query_texts=queries, n_results=n_results, include=["documents", "distances", "metadatas"])

    ids_by_query       = results.get("ids", [])
    docs_by_query      = results.get("documents", [])
    distances_by_query = results.get("distances", [])
    meta_by_query      = results.get("metadatas", [])

    # window limit -> list of (original tokens, excerpt tokens, L2 delta)
    stats: dict[int, list[tuple[int, int, float]]] = {l: [] for l in window_limits}

    for q_idx, query in enumerate(queries):
        print("=" * 72)
        print(f"Query {q_idx + 1}: \"{query}\"")
        print("=" * 72)

        ids       = ids_by_query[q_idx]       if q_idx < len(ids_by_query)       else []
        docs      = docs_by_query[q_idx]      if q_idx < len(docs_by_query)      else []
        distances = distances_by_query[q_idx] if q_idx < len(distances_by_query) else []
        metas     = meta_by_query[q_idx]      if q_idx < len(meta_by_query)      else []

        if not ids:
            print("  (no results)\n")
            continue

        # One batched windowing pass per limit, so the embedding calls match
        # what retrieval.py actually issues (all candidates at once).
        pairs = [(doc, meta or {}) for doc, meta in zip(docs, metas)]
        windowed = {limit: window_pass(query, pairs, limit) for limit in window_limits}

        for rank, (chunk_id, doc, dist, meta) in enumerate(zip(ids, docs, distances, metas), start=1):
            sim = cosine_sim(dist)
            tag = label(dist)
            source = meta.get("source", "") if meta else ""
            full_tokens = count_tokens(doc)
            print(f"\n  [{rank}] {chunk_id}  |  L2={dist:.4f}  cos_sim={sim:.4f}  ({tag})  {full_tokens} tok")
            if source:
                print(f"       source: {source}")
            if not (brief and window_limits):
                print(indent(doc))

            for limit in window_limits:
                excerpt, w_dist = windowed[limit][rank - 1]
                w_tokens = count_tokens(excerpt)
                delta = w_dist - dist
                unchanged = " (unchanged)" if excerpt.strip() == doc.strip() else ""
                stats[limit].append((full_tokens, w_tokens, delta))
                print(f"\n       -- window {limit} tok --> {w_tokens} tok"
                      f"  L2={w_dist:.4f} ({delta:+.4f})  cos_sim={cosine_sim(w_dist):.4f}"
                      f"  ({label(w_dist)}){unchanged}")
                if not unchanged:
                    print(indent(excerpt, "         "))

        print()

    # Summary: unique chunks across all queries with best (lowest) distance
    if len(queries) > 1:
        print("=" * 72)
        print("Summary: all unique chunks ranked by best L2 distance")
        print("=" * 72)
        best: dict[str, tuple[float, str]] = {}
        for ids, docs, distances in zip(ids_by_query, docs_by_query, distances_by_query):
            for chunk_id, doc, dist in zip(ids, docs, distances):
                if chunk_id not in best or dist < best[chunk_id][0]:
                    best[chunk_id] = (dist, doc)
        for rank, (chunk_id, (dist, doc)) in enumerate(
            sorted(best.items(), key=lambda kv: kv[1][0]), start=1
        ):
            sim = cosine_sim(dist)
            tag = label(dist)
            print(f"  [{rank}] {chunk_id}  L2={dist:.4f}  cos_sim={sim:.4f}  ({tag})")
        print()

    if window_limits:
        print("=" * 72)
        print("Summary: windowing effect (across every result above)")
        print("=" * 72)
        for limit, rows in stats.items():
            if not rows:
                continue
            before = sum(r[0] for r in rows)
            after = sum(r[1] for r in rows)
            deltas = [r[2] for r in rows]
            improved = sum(1 for d in deltas if d < 0)
            print(f"  window {limit:>4} tok:  {before} -> {after} tokens "
                  f"({100 * (1 - after / before):.0f}% smaller)  |  "
                  f"mean L2 delta {sum(deltas) / len(deltas):+.4f}, "
                  f"closer to query in {improved}/{len(deltas)} chunks")
        print("\n  (negative L2 delta = the excerpt is closer to the query than the whole\n"
              "   chunk was, i.e. the window cut away off-topic text)\n")


def parse_windows(raw: str | None) -> list[int]:
    """`--window` value -> token limits. Bare flag means WINDOW_TOKEN_LIMIT."""
    if raw is None:
        return []
    if raw == "":
        return [WINDOW_TOKEN_LIMIT]
    return [int(part) for part in raw.split(",") if part.strip()]


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Query ChromaDB and inspect L2 distances for threshold tuning."
    )
    parser.add_argument("queries", nargs="+", help="One or more query strings")
    parser.add_argument(
        "--n", type=int, default=CHROMA_N_RESULTS,
        help=f"Number of results per query (default: {CHROMA_N_RESULTS})"
    )
    parser.add_argument(
        "--window", nargs="?", const="", default=None, metavar="TOKENS",
        help="Also show the shared/windowing.py excerpt at this token limit. "
             f"Comma-separate to compare several (e.g. 50,100,200). "
             f"Bare --window uses WINDOW_TOKEN_LIMIT={WINDOW_TOKEN_LIMIT}."
    )
    parser.add_argument(
        "--brief", action="store_true",
        help="With --window, hide the full chunk text and print only the excerpts."
    )
    args = parser.parse_args()
    run(args.queries, args.n, parse_windows(args.window), args.brief)


if __name__ == "__main__":
    main()
