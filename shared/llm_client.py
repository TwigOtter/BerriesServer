"""
shared/llm_client.py

LLM abstraction layer — swap between Anthropic API and local Ollama
by changing LLM_BACKEND in config (or .env) without touching the callers.

Every call is timed and logged with its purpose, model, and token usage, and
recorded into the active interaction trace (shared/trace.py) when one exists.

Usage:
    from shared.llm_client import get_completion
    reply = await get_completion(system_prompt="You are Berries...", user_message="say hi")
"""

import logging
import time

from shared import trace
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
    purpose: str = "chat",
) -> str:
    """
    Send a prompt to the configured LLM backend and return the response text.
    Raises ValueError if LLM_BACKEND is not recognized.

    model:    override the model used for this call. Defaults to ANTHROPIC_CHAT_MODEL (Sonnet).
              Pass ANTHROPIC_ASSIST_MODEL explicitly for utility tasks (query rewriting, gif queries, etc.).
    messages: full conversation history as a list of {"role": ..., "content": ...} dicts.
              When provided, takes precedence over user_message.
    purpose:  short label for what this call is for ("chat_response",
              "rewrite_queries", "rerank", ...) — appears in logs and traces.
    """
    resolved_messages = messages or [{"role": "user", "content": user_message}]
    resolved_model = (model or ANTHROPIC_CHAT_MODEL) if LLM_BACKEND == "anthropic" else OLLAMA_MODEL

    t0 = time.perf_counter()
    usage: dict = {}
    error: str | None = None
    try:
        if LLM_BACKEND == "anthropic":
            text, usage = await _anthropic_completion(system_prompt, resolved_messages, max_tokens, resolved_model)
        elif LLM_BACKEND == "ollama":
            text, usage = await _ollama_completion(system_prompt, resolved_messages, max_tokens)
        else:
            raise ValueError(f"Unknown LLM_BACKEND: {LLM_BACKEND!r}. Use 'anthropic' or 'ollama'.")
        return text
    except Exception as e:
        error = f"{type(e).__name__}: {e}"
        raise
    finally:
        ms = (time.perf_counter() - t0) * 1000
        if error:
            _log.warning(
                "LLM call failed — purpose=%s model=%s backend=%s %.2fs: %s",
                purpose, resolved_model, LLM_BACKEND, ms / 1000, error,
            )
        else:
            _log.info(
                "LLM call — purpose=%s model=%s %.2fs in=%s out=%s",
                purpose, resolved_model, ms / 1000,
                usage.get("input_tokens", "?"), usage.get("output_tokens", "?"),
            )
        trace.record_llm_call(
            purpose=purpose,
            model=resolved_model,
            backend=LLM_BACKEND,
            ms=ms,
            input_tokens=usage.get("input_tokens"),
            output_tokens=usage.get("output_tokens"),
            max_tokens=max_tokens,
            error=error,
        )


async def _anthropic_completion(
    system_prompt: str, messages: list[dict], max_tokens: int, model: str
) -> tuple[str, dict]:
    import anthropic
    async with anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY) as client:
        message = await client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system_prompt,
            messages=messages,
        )
        usage = {
            "input_tokens": message.usage.input_tokens,
            "output_tokens": message.usage.output_tokens,
        }
        return message.content[0].text, usage


async def _ollama_completion(
    system_prompt: str, messages: list[dict], max_tokens: int
) -> tuple[str, dict]:
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
        data = resp.json()
        usage = {
            "input_tokens": data.get("prompt_eval_count"),
            "output_tokens": data.get("eval_count"),
        }
        return data["message"]["content"], usage
