"""
berries_bot/main.py

Receives a response trigger (from ingest_api), assembles context from the
recent_chunks deque and ChromaDB, calls the LLM, and posts to Twitch chat
via a Streamer.bot callback.

This service is triggered by ingest_api — it doesn't poll for work.
For now it exposes a lightweight HTTP endpoint so ingest_api can call it directly.

Run with:
    uvicorn berries_bot.main:app --host 127.0.0.1 --port 8001
"""

import httpx
from collections import deque
from fastapi import FastAPI, Request

from shared.config import (
    CHROMA_N_RESULTS,
    PERSONALITY_FILE,
    STREAMERBOT_CALLBACK_URL,
)
from shared.chroma_client import get_collection
from shared.llm_client import get_completion
from shared.prompt_builder import build_system_prompt, ContextType

app = FastAPI(title="Berries Bot")

# Populated by ingest_api (shared reference in single-process dev mode,
# or reconstructed from ChromaDB in multi-process deployment).
recent_chunks: deque = deque(maxlen=2)

# ── Personality ────────────────────────────────────────────────────────────

def _load_personality() -> str:
    """Load Berries' system prompt from file."""
    if PERSONALITY_FILE.exists():
        return PERSONALITY_FILE.read_text(encoding="utf-8").strip()
    return "You are Berries, a playful forest demon on a Twitch stream."


# ── Context assembly ───────────────────────────────────────────────────────

def _assemble_context(trigger_text: str) -> str:
    """
    Build the context block to append to Berries' system prompt.
    1. Last 2 chunks from deque (short-term memory)
    2. 3–5 semantically similar chunks from ChromaDB (long-term memory)
    """
    parts: list[str] = []

    # Short-term: recent chunks
    if recent_chunks:
        parts.append("=== RECENT STREAM CONTEXT ===")
        for chunk in recent_chunks:
            parts.append(chunk.get("text", ""))

    # Long-term: ChromaDB similarity search
    collection = get_collection()
    results = collection.query(query_texts=[trigger_text], n_results=CHROMA_N_RESULTS)
    docs = results.get("documents", [[]])[0]
    if docs:
        parts.append("\n=== RELEVANT PAST CONTEXT ===")
        parts.extend(docs)

    return "\n\n".join(parts)


# ── Response pipeline ──────────────────────────────────────────────────────

async def generate_response(trigger_text: str) -> str:
    """Full pipeline: assemble context → call LLM → return response text."""
    personality = _load_personality()
    context = _assemble_context(trigger_text)

    system_prompt = build_system_prompt(personality, ContextType.TWITCH_CHAT, context)
    return await get_completion(system_prompt=system_prompt, user_message=trigger_text)


async def post_to_twitch(message: str) -> None:
    """Send Berries' response back to Streamer.bot for posting in Twitch chat."""
    if not STREAMERBOT_CALLBACK_URL:
        print(f"[berries_bot] (no callback URL) Would post: {message}")
        return
    async with httpx.AsyncClient() as client:
        await client.post(STREAMERBOT_CALLBACK_URL, json={"message": message}, timeout=5.0)


# ── Routes ─────────────────────────────────────────────────────────────────

@app.post("/respond")
async def respond(request: Request) -> dict:
    """
    Trigger Berries to generate and post a response.

    Expected body:
        {"trigger_text": "the message or context that prompted a response"}
    """
    body = await request.json()
    trigger_text = body.get("trigger_text", "")

    if not trigger_text:
        return {"status": "error", "detail": "trigger_text required"}

    response_text = await generate_response(trigger_text)
    await post_to_twitch(response_text)

    return {"status": "ok", "response": response_text}


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "recent_chunks": len(recent_chunks)}
