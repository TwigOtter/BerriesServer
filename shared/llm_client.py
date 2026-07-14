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
    ANTHROPIC_API_KEY, ANTHROPIC_ASSIST_MODEL, ANTHROPIC_CHAT_MODEL,
    OLLAMA_BASE_URL, OLLAMA_MODEL,
)

_log = logging.getLogger("llm_client")


async def get_completion(
    system_prompt: str,
    user_message: str = "",
    max_tokens: int = 256,
    model: str | None = None,
    messages: list[dict] | None = None,
) -> str:
    """
    Send a prompt to the configured LLM backend and return the response text.
    Raises ValueError if LLM_BACKEND is not recognized.

    model:    override the model used for this call. Defaults to ANTHROPIC_CHAT_MODEL (Sonnet).
              Pass ANTHROPIC_ASSIST_MODEL explicitly for utility tasks (query rewriting, gif queries, etc.).
    messages: full conversation history as a list of {"role": ..., "content": ...} dicts.
              When provided, takes precedence over user_message.
    """
    resolved_messages = messages or [{"role": "user", "content": user_message}]
    if LLM_BACKEND == "anthropic":
        return await _anthropic_completion(system_prompt, resolved_messages, max_tokens, model or ANTHROPIC_CHAT_MODEL)
    elif LLM_BACKEND == "ollama":
        return await _ollama_completion(system_prompt, resolved_messages, max_tokens)
    else:
        raise ValueError(f"Unknown LLM_BACKEND: {LLM_BACKEND!r}. Use 'anthropic' or 'ollama'.")


async def _anthropic_completion(system_prompt: str, messages: list[dict], max_tokens: int, model: str) -> str:
    import anthropic
    async with anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY) as client:
        message = await client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system_prompt,
            messages=messages,
        )
        return message.content[0].text


async def _ollama_completion(system_prompt: str, messages: list[dict], max_tokens: int) -> str:
    import httpx
    payload = {
        "model": OLLAMA_MODEL,
        "messages": [{"role": "system", "content": system_prompt}, *messages],
        "stream": False,
        "options": {"num_predict": max_tokens},
    }
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{OLLAMA_BASE_URL}/api/chat",
            json=payload,
            timeout=120.0,
        )
        resp.raise_for_status()
        return resp.json()["message"]["content"]
