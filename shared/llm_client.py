"""
shared/llm_client.py

LLM abstraction layer — swap between Anthropic API and local Ollama
by changing LLM_BACKEND in config (or .env) without touching the callers.

Usage:
    from shared.llm_client import get_completion
    reply = await get_completion(system_prompt="You are Berries...", user_message="say hi")
"""

import logging

from shared.config import (
    LLM_BACKEND,
    ANTHROPIC_API_KEY, ANTHROPIC_MODEL,
    OLLAMA_BASE_URL, OLLAMA_MODEL,
)

_log = logging.getLogger("llm_client")

_REWRITER_MODEL = "claude-haiku-4-5-20251001"


async def rewrite_queries(
    message: str,
    recent_context: str,
    username: str = "a viewer",
) -> list[str] | None:
    """
    Use Haiku to rewrite `message` into 2-3 focused ChromaDB search queries.
    Returns None if the model says SKIP (no retrieval needed).
    Falls back to [message] on any error or if ANTHROPIC_API_KEY is not set.
    """
    if not ANTHROPIC_API_KEY:
        return [message]

    prompt = (
        f"Given this recent chat context:\n{recent_context}\n\n"
        f"And this message from {username}:\n\"{message}\"\n\n"
        "Generate 2-3 distinct search queries (one per line, no labels or punctuation) "
        "that capture what information about the user and their query would be "
        "most useful to retrieve in order to respond well to this message. "
        "If the message needs no factual retrieval (e.g. pure banter, greetings), "
        "return only the word: SKIP"
    )

    try:
        import anthropic
        client = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
        msg = await client.messages.create(
            model=_REWRITER_MODEL,
            max_tokens=128,
            system="You generate ChromaDB search queries. Follow the instructions exactly.",
            messages=[{"role": "user", "content": prompt}],
        )
        raw = msg.content[0].text.strip()
        if raw.upper() == "SKIP":
            return None
        queries = [q.strip() for q in raw.splitlines() if q.strip()]
        return queries if queries else [message]
    except Exception as e:
        _log.warning("rewrite_queries failed, falling back to raw message: %s", e)
        return [message]


async def get_completion(system_prompt: str, user_message: str, max_tokens: int = 256) -> str:
    """
    Send a prompt to the configured LLM backend and return the response text.
    Raises ValueError if LLM_BACKEND is not recognized.
    """
    if LLM_BACKEND == "anthropic":
        return await _anthropic_completion(system_prompt, user_message, max_tokens)
    elif LLM_BACKEND == "ollama":
        return await _ollama_completion(system_prompt, user_message, max_tokens)
    else:
        raise ValueError(f"Unknown LLM_BACKEND: {LLM_BACKEND!r}. Use 'anthropic' or 'ollama'.")


async def _anthropic_completion(system_prompt: str, user_message: str, max_tokens: int) -> str:
    import anthropic
    client = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
    message = await client.messages.create(
        model=ANTHROPIC_MODEL,
        max_tokens=max_tokens,
        system=system_prompt,
        messages=[{"role": "user", "content": user_message}],
    )
    return message.content[0].text


async def _ollama_completion(system_prompt: str, user_message: str, max_tokens: int) -> str:
    import httpx
    payload = {
        "model": OLLAMA_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ],
        "stream": False,
        "options": {"num_predict": max_tokens},
    }
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{OLLAMA_BASE_URL}/api/chat",
            json=payload,
            timeout=30.0,
        )
        resp.raise_for_status()
        return resp.json()["message"]["content"]
