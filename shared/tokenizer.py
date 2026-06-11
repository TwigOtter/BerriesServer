"""
shared/tokenizer.py

Token counting utility used by ingest_api to decide when to flush the buffer.
Uses tiktoken with the cl100k_base encoding (compatible with most modern models).
"""

import tiktoken

# Loaded lazily — tiktoken fetches the encoding file on first use, so doing
# this at import time makes every importer pay the cost (and fail offline).
_enc: tiktoken.Encoding | None = None


def _get_enc() -> tiktoken.Encoding:
    global _enc
    if _enc is None:
        _enc = tiktoken.get_encoding("cl100k_base")
    return _enc


def count_tokens(text: str) -> int:
    """Return the number of tokens in a string."""
    return len(_get_enc().encode(text))


def count_tokens_for_messages(messages: list[dict]) -> int:
    """
    Rough token count for a list of message dicts with a 'text' key.
    Useful for estimating buffer size before flushing.
    """
    return sum(count_tokens(m.get("text", "")) for m in messages)
