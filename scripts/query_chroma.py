"""
scripts/query_chroma.py

Interactive CLI tool for testing ChromaDB queries and inspecting L2 distances.

Usage (from repo root, with venv active):
    python scripts/query_chroma.py "what did twig say about disc golf"
    python scripts/query_chroma.py "disc golf" "dark souls" "movie night"
    python scripts/query_chroma.py --n 6 "some query"
"""

import argparse
import sys
import textwrap

# Allow imports from repo root
sys.path.insert(0, __import__("pathlib").Path(__file__).resolve().parent.parent.__str__())

from shared.chroma_client import get_collection
from shared.config import CHROMA_N_RESULTS


def cosine_sim(l2: float) -> float:
    """Convert L2 distance (normalized vectors) to cosine similarity."""
    return 1.0 - (l2 ** 2) / 2.0


def label(l2: float) -> str:
    sim = cosine_sim(l2)
    if sim >= 0.75:
        return "strong"
    if sim >= 0.5:
        return "moderate"
    if sim >= 0.25:
        return "weak"
    return "noise"


def run(queries: list[str], n_results: int) -> None:
    print(f"\nQuerying ChromaDB  (n_results={n_results} per query)\n")
    collection = get_collection()
    results = collection.query(query_texts=queries, n_results=n_results, include=["documents", "distances", "metadatas"])

    ids_by_query       = results.get("ids", [])
    docs_by_query      = results.get("documents", [])
    distances_by_query = results.get("distances", [])
    meta_by_query      = results.get("metadatas", [])

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

        for rank, (chunk_id, doc, dist, meta) in enumerate(zip(ids, docs, distances, metas), start=1):
            sim = cosine_sim(dist)
            tag = label(dist)
            source = meta.get("source", "") if meta else ""
            print(f"\n  [{rank}] {chunk_id}  |  L2={dist:.4f}  cos_sim={sim:.4f}  ({tag})")
            if source:
                print(f"       source: {source}")
            # Wrap the document text at 68 chars, indented
            wrapped = textwrap.fill(doc.strip(), width=68, initial_indent="       ", subsequent_indent="       ")
            print(wrapped)

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


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Query ChromaDB and inspect L2 distances for threshold tuning."
    )
    parser.add_argument("queries", nargs="+", help="One or more query strings")
    parser.add_argument(
        "--n", type=int, default=CHROMA_N_RESULTS,
        help=f"Number of results per query (default: {CHROMA_N_RESULTS})"
    )
    args = parser.parse_args()
    run(args.queries, args.n)


if __name__ == "__main__":
    main()
