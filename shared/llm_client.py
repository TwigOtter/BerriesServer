"""
shared/llm_client.py

LLM abstraction layer — swap between the Anthropic API, local Ollama, or a
vLLM server (OpenAI-compatible API) by changing LLM_BACKEND in config (or
.env) without touching the callers.

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
    VLLM_BASE_URL, VLLM_CHAT_MODEL,
    VLLM_TEMPERATURE, VLLM_TOP_P, VLLM_TOP_K, VLLM_REPETITION_PENALTY,
)

_log = logging.getLogger("llm_client")


def fold_developer_blocks(messages: list[dict]) -> list[dict]:
    """
    Fold role="developer" messages into the content of the next role="user"
    message (prepended, delimited by a blank line) -- for backends with no
    developer role that also require strict user/assistant alternation
    (Anthropic, Ollama). This preserves positional intent -- e.g. a developer
    block placed immediately before the final query stays adjacent to that
    query by becoming part of its content -- instead of yanking every
    developer block up to the system prompt regardless of where it belongs.

    Trailing developer content with no following user message (shouldn't
    happen with current callers, which always end on a user turn) is merged
    into the last user message if one exists, else becomes its own user
    message.
    """
    result: list[dict] = []
    pending: list[str] = []
    for msg in messages:
        if msg["role"] == "developer":
            pending.append(msg["content"])
            continue
        if pending and msg["role"] == "user":
            msg = {**msg, "content": "\n\n".join([*pending, msg["content"]])}
            pending = []
        result.append(msg)
    if pending:
        _log.warning("fold_developer_blocks: trailing developer content with no following user message")
        if result and result[-1]["role"] == "user":
            result[-1] = {**result[-1], "content": result[-1]["content"] + "\n\n" + "\n\n".join(pending)}
        else:
            result.append({"role": "user", "content": "\n\n".join(pending)})
    return result


def merge_consecutive_messages(messages: list[dict]) -> list[dict]:
    """
    Merge consecutive same-role messages (content joined with "\n") into one.
    Applied for every backend -- not just Anthropic, which rejects
    consecutive same-role messages outright -- so the message shape sent
    doesn't silently depend on LLM_BACKEND.
    """
    merged: list[dict] = []
    for msg in messages:
        if merged and merged[-1]["role"] == msg["role"]:
            merged[-1] = {**merged[-1], "content": merged[-1]["content"] + "\n" + msg["content"]}
        else:
            merged.append(dict(msg))
    return merged


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
    messages: full conversation as a list of {"role": ..., "content": ...} dicts.
              role can be "user", "assistant", or "developer" -- a context
              block (retrieval results, user profile, ...) positioned at a
              specific point in the conversation, e.g. right before the final
              query. When provided, takes precedence over user_message.

              Before dispatch, "developer" entries are folded into the next
              "user" message for backends without a developer role
              (fold_developer_blocks) -- vLLM accepts "developer" natively, so
              it's skipped there. Consecutive same-role messages are then
              merged (merge_consecutive_messages) for every backend.
    purpose:  short label for what this call is for ("chat_response",
              "rewrite_queries", "rerank", ...) — appears in logs and traces.
    """
    resolved_messages = messages or [{"role": "user", "content": user_message}]
    if LLM_BACKEND != "vllm":
        resolved_messages = fold_developer_blocks(resolved_messages)
    resolved_messages = merge_consecutive_messages(resolved_messages)
    if LLM_BACKEND == "anthropic":
        resolved_model = model or ANTHROPIC_CHAT_MODEL
    elif LLM_BACKEND == "vllm":
        resolved_model = VLLM_CHAT_MODEL
    else:
        resolved_model = OLLAMA_MODEL

    t0 = time.perf_counter()
    usage: dict = {}
    error: str | None = None
    try:
        if LLM_BACKEND == "anthropic":
            text, usage = await _anthropic_completion(system_prompt, resolved_messages, max_tokens, resolved_model)
        elif LLM_BACKEND == "ollama":
            text, usage = await _ollama_completion(system_prompt, resolved_messages, max_tokens)
        elif LLM_BACKEND == "vllm":
            text, usage = await _vllm_completion(system_prompt, resolved_messages, max_tokens)
        else:
            raise ValueError(f"Unknown LLM_BACKEND: {LLM_BACKEND!r}. Use 'anthropic', 'ollama', or 'vllm'.")
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


async def _vllm_completion(
    system_prompt: str, messages: list[dict], max_tokens: int,
) -> tuple[str, dict]:
    import httpx
    # messages may already contain role="developer" entries at their correct
    # position (vLLM accepts that role natively) -- see get_completion().
    payload_messages = [{"role": "system", "content": system_prompt}, *messages]
    payload = {
        "model": VLLM_CHAT_MODEL,
        "messages": payload_messages,
        "max_tokens": max_tokens,
        "stream": False,
        # Sampling params (config-tunable). repetition_penalty > 1.0 is what
        # discourages the model from lifting an earlier in-context turn verbatim
        # -- in vLLM it penalizes prompt tokens, not just generated ones.
        # top_k/repetition_penalty are vLLM extensions to the OpenAI schema.
        "temperature": VLLM_TEMPERATURE,
        "top_p": VLLM_TOP_P,
        "top_k": VLLM_TOP_K,
        "repetition_penalty": VLLM_REPETITION_PENALTY,
        # Qwen3-family models think by default and will burn the entire
        # max_tokens budget on chain-of-thought before answering. Templates
        # that don't know this kwarg silently ignore it.
        "chat_template_kwargs": {"enable_thinking": False},
    }
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{VLLM_BASE_URL}/v1/chat/completions",
            json=payload,
            timeout=120.0,
        )
        resp.raise_for_status()
        data = resp.json()
        text = data["choices"][0]["message"]["content"] or ""
        # Reasoning models (Qwen3 family) emit their thinking inline as a
        # <think>...</think> block unless the server strips it; only the part
        # after the block is the actual reply.
        if "</think>" in text:
            text = text.split("</think>", 1)[1].strip()
        usage = {
            "input_tokens": data.get("usage", {}).get("prompt_tokens"),
            "output_tokens": data.get("usage", {}).get("completion_tokens"),
        }
        return text, usage


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
