"""
shared/llm_client.py

LLM abstraction layer — swap between Anthropic API and local Ollama
by changing LLM_BACKEND in config (or .env) without touching the callers.

Usage:
    from shared.llm_client import get_completion
    reply = await get_completion(system_prompt="You are Berries...", user_message="say hi")
"""

from shared.config import (
    LLM_BACKEND,
    ANTHROPIC_API_KEY, ANTHROPIC_MODEL,
    OLLAMA_BASE_URL, OLLAMA_MODEL,
)


async def get_completion(system_prompt: str, user_message: str) -> str:
    """
    Send a prompt to the configured LLM backend and return the response text.
    Raises ValueError if LLM_BACKEND is not recognized.
    """
    if LLM_BACKEND == "anthropic":
        return await _anthropic_completion(system_prompt, user_message)
    elif LLM_BACKEND == "ollama":
        return await _ollama_completion(system_prompt, user_message)
    else:
        raise ValueError(f"Unknown LLM_BACKEND: {LLM_BACKEND!r}. Use 'anthropic' or 'ollama'.")


async def _anthropic_completion(system_prompt: str, user_message: str) -> str:
    import anthropic
    client = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
    message = await client.messages.create(
        model=ANTHROPIC_MODEL,
        max_tokens=256,
        system=system_prompt,
        messages=[{"role": "user", "content": user_message}],
    )
    return message.content[0].text


async def _ollama_completion(system_prompt: str, user_message: str) -> str:
    import httpx
    payload = {
        "model": OLLAMA_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ],
        "stream": False,
    }
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{OLLAMA_BASE_URL}/api/chat",
            json=payload,
            timeout=30.0,
        )
        resp.raise_for_status()
        return resp.json()["message"]["content"]
