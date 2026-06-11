"""
shared/retrieval_log.py

Daily retrieval log — one JSON file per calendar day.

File: logs/daily_interactions/YYYY-MM-DD_retrievals.json
Schema:
  {
    "<original user query>": ["chunk text 1", "chunk text 2", ...]
  }

Keys are the original user messages that triggered a ChromaDB lookup.
Values are the raw chunk texts that were actually injected into the prompt
(post-reranking — see shared/retrieval.py).

On key collision, the most recent retrieval overwrites — most recent L2 matches
are the best matches, so newer results are preferred.

Summary chunks (source: summary) must be pre-filtered by the caller; only raw
source chunks should be logged here to prevent summaries from being re-summarized.

Thread safety: same file-lock pattern as interaction_log.py.
"""

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from shared.config import LOGS_DIR

_INTERACTIONS_DIR = LOGS_DIR / "daily_interactions"
_MAX_CHUNKS = 4


def _today_path() -> Path:
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    _INTERACTIONS_DIR.mkdir(parents=True, exist_ok=True)
    return _INTERACTIONS_DIR / f"{date_str}_retrievals.json"


def _load(path: Path) -> dict[str, list[str]]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save(path: Path, data: dict[str, list[str]]) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def log_retrieval(query: str, chunks: list[str]) -> None:
    """
    Record a retrieval event: query → up to 4 raw chunk texts.
    Overwrites any existing entry for this query (most recent wins).

    query:  the original user message that triggered the ChromaDB lookup
    chunks: raw chunk texts to store (pre-filtered to exclude source:summary)
    """
    if not query or not chunks:
        return

    path = _today_path()
    lock = path.with_suffix(".lock")

    fd = None
    try:
        for _ in range(20):
            try:
                fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                break
            except FileExistsError:
                import time
                time.sleep(0.1)
        if fd is None:
            data = _load(path)
            data[query] = chunks[:_MAX_CHUNKS]
            _save(path, data)
            return
        os.close(fd)
        data = _load(path)
        data[query] = chunks[:_MAX_CHUNKS]
        _save(path, data)
    finally:
        try:
            lock.unlink(missing_ok=True)
        except Exception:
            pass
