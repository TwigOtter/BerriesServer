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
    try:
        ids = [item["id"] for item in pending]
        docs = [item["document"] for item in pending]
        metas = [item["metadata"] for item in pending]
        get_collection().upsert(ids=ids, documents=docs, metadatas=metas)
    except Exception:
        log.exception("Upsert failed")
        return 2

    log.info("Upsert succeeded: %d summaries written", len(pending))
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: upsert_pending.py <pending_summaries_json_path>", file=sys.stderr)
        sys.exit(1)
    sys.exit(main(Path(sys.argv[1])))
