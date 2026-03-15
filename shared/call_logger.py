"""
shared/call_logger.py

Rotating JSONL log of LLM call chains, one record per line.
Captures: query-rewriting output, full system prompt, user message, and response.

Log location: logs/llm_calls.jsonl  (5 MB, 1 backup → ~10 MB max)
"""

import json
import logging
import logging.handlers
from datetime import datetime, timezone

from shared.config import LOGS_DIR

LOGS_DIR.mkdir(parents=True, exist_ok=True)

_handler = logging.handlers.RotatingFileHandler(
    LOGS_DIR / "llm_calls.jsonl",
    maxBytes=5 * 1024 * 1024,
    backupCount=1,
    encoding="utf-8",
)
_handler.setFormatter(logging.Formatter("%(message)s"))

_log = logging.getLogger("llm_calls")
_log.setLevel(logging.DEBUG)
_log.addHandler(_handler)
_log.propagate = False  # don't bleed into root/service loggers


def log_llm_call(
    *,
    service: str,
    username: str,
    raw_message: str,
    rewrite_queries: list[str] | None,
    system_prompt: str,
    user_message: str,
    response: str,
) -> None:
    """
    Write one JSONL record for a complete LLM call chain.

    rewrite_queries:
      - list of strings  → queries sent to ChromaDB
      - None             → rewriter returned SKIP (ChromaDB bypassed)
      - pass the string "error" if rewriting failed and fell back to raw message
    """
    record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "service": service,
        "username": username,
        "raw_message": raw_message,
        "rewrite_queries": rewrite_queries,
        "system_prompt": system_prompt,
        "user_message": user_message,
        "response": response,
    }
    _log.debug(json.dumps(record, ensure_ascii=False))
