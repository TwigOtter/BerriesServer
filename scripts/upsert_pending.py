"""
scripts/upsert_pending.py

Standalone upsert for pending summary JSON files produced by dream.py Phase 3.

Runs as a *subprocess* of dream.py to give ChromaDB's Rust backend a fresh
Python interpreter with no inherited state from asyncio. Calling
collection.upsert() in dream.py's main process segfaults reliably after
asyncio.run() has executed; the same call in a freshly-spawned interpreter
is segfault-free (same context as scripts/reindex_*.py).

Usage:
    python scripts/upsert_pending.py <pending_summaries_json_path>

Exit codes:
    0 — upsert succeeded (or pending list was empty)
    1 — usage error / file not found
    2 — upsert raised an exception
"""

import json
import logging
import os
import sys
from pathlib import Path

# Match dream.py's env setup so the subprocess runs under the same constraints.
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("MKL_NUM_THREADS", "1")
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
os.environ.setdefault("ANONYMIZED_TELEMETRY", "False")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from shared.chroma_client import get_collection

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("upsert_pending")


def main(pending_path: Path) -> int:
    if not pending_path.exists():
        log.error("Pending file not found: %s", pending_path)
        return 1

    try:
        pending = json.loads(pending_path.read_text(encoding="utf-8"))
    except Exception:
        log.exception("Failed to parse pending summaries at %s", pending_path)
        return 2

    if not pending:
        log.info("No pending summaries in %s — nothing to upsert", pending_path)
        return 0

    log.info("Upserting %d summaries to ChromaDB", len(pending))
    ids = [item["id"] for item in pending]
    try:
        docs = [item["document"] for item in pending]
        metas = [item["metadata"] for item in pending]
        get_collection().upsert(ids=ids, documents=docs, metadatas=metas)
    except Exception:
        log.exception("Upsert failed")
        return 2

    log.info("Upsert succeeded: %d summaries written", len(pending))

    # Delete the chunks that were folded into those summaries — strictly after
    # the upsert, so a failure anywhere above leaves the originals intact. The
    # worst case here is a summary that co-exists with its sources (redundant
    # but lossless); deleting first would risk losing both.
    #
    # Summary IDs are excluded defensively: dream.py already filters a summary
    # out of its own source list, but a stale pending file from before that
    # guard existed must not be able to delete a summary this run just wrote.
    written = set(ids)
    source_ids = sorted({
        cid
        for item in pending
        for cid in item.get("source_ids", [])
        if cid not in written
    })
    if not source_ids:
        log.info("No source chunks to delete")
        return 0

    try:
        get_collection().delete(ids=source_ids)
    except Exception:
        # Non-fatal: the summaries are already written, so returning 0 lets
        # dream.py drop the pending file. Re-running would otherwise re-spend
        # LLM tokens to re-derive summaries that already exist.
        log.exception("Deleting %d consolidated source chunk(s) failed", len(source_ids))
        return 0

    log.info("Deleted %d consolidated source chunk(s)", len(source_ids))
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: upsert_pending.py <pending_summaries_json_path>", file=sys.stderr)
        sys.exit(1)
    sys.exit(main(Path(sys.argv[1])))
