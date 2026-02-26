"""
ingest_api/main.py

FastAPI front door. Receives all events from Streamer.bot via HTTP POST,
preprocesses them, manages the chunking buffer, embeds and stores chunks,
and fans out triggers to berries_bot and stream_utils.

Run with:
    uvicorn ingest_api.main:app --host 0.0.0.0 --port 8000
"""

import asyncio
import json
import time
import uuid
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

import httpx
from fastapi import FastAPI, Header, HTTPException, Request

from shared.config import (
    CHUNK_OVERLAP_SEC,
    CHUNK_TIMEOUT_SEC,
    CHUNK_TOKEN_LIMIT,
    INGEST_SECRET,
    STREAMERBOT_CALLBACK_URL,
    TRANSCRIPTS_DIR,
)
def get_collection():
    from shared.chroma_client import get_collection as _get_collection
    return _get_collection()

def count_tokens(text: str) -> int:
    from shared.tokenizer import count_tokens as _count_tokens
    return _count_tokens(text)

app = FastAPI(title="Berries Ingest API")

# ── In-memory state ────────────────────────────────────────────────────────
# Each entry: {"speaker": str, "text": str, "timestamp": float}
_buffer: list[dict] = []
_last_event_time: float = time.time()

# Shared deque for recent chunks — berries_bot reads from this for short-term memory
recent_chunks: deque = deque(maxlen=2)

# ── Helpers ────────────────────────────────────────────────────────────────

def _auth_check(x_secret: str | None) -> None:
    """Reject requests that don't carry the shared secret (if configured)."""
    if INGEST_SECRET and x_secret != INGEST_SECRET:
        raise HTTPException(status_code=403, detail="Forbidden")


def _preprocess_message(speaker: str, text: str) -> str | None:
    """
    Clean a single message before buffering.
    Returns None if the message should be dropped entirely.
    """
    text = text.strip()

    # Drop empty, single-character, or bot-command messages
    if len(text) <= 1 or text.startswith("!"):
        return None

    # TODO: condense emote spam — e.g. "PogChamp PogChamp PogChamp" → "PogChamp_x3"
    # TODO: add more noise filters as patterns emerge from real transcripts

    return f"[{speaker}]: {text}"


def _buffer_text() -> str:
    return "\n".join(e["text"] for e in _buffer)


def _buffer_token_count() -> int:
    return count_tokens(_buffer_text())


async def _flush_buffer(reason: str) -> None:
    """
    Flush the current buffer to .jsonl, ChromaDB, and the recent_chunks deque.
    Keeps the last CHUNK_OVERLAP_SEC seconds of entries as the next buffer seed.
    """
    global _buffer

    if not _buffer:
        return

    now = datetime.now(timezone.utc)
    stream_date = now.strftime("%Y-%m-%d")
    chunk_id = f"{now.strftime('%Y-%m-%dT%H-%M-%S')}_{uuid.uuid4().hex[:6]}"

    start_ts = datetime.fromtimestamp(_buffer[0]["timestamp"], tz=timezone.utc).isoformat()
    end_ts = datetime.fromtimestamp(_buffer[-1]["timestamp"], tz=timezone.utc).isoformat()
    text = _buffer_text()
    token_count = count_tokens(text)
    speakers = list(dict.fromkeys(e["speaker"] for e in _buffer))  # ordered unique

    chunk = {
        "chunk_id": chunk_id,
        "stream_date": stream_date,
        "start_time": start_ts,
        "end_time": end_ts,
        "flush_reason": reason,
        "text": text,
        "token_count": token_count,
        "speaker_summary": speakers,
    }

    # 1. Write to .jsonl (ground truth first)
    TRANSCRIPTS_DIR.mkdir(parents=True, exist_ok=True)
    jsonl_path = TRANSCRIPTS_DIR / f"stream_chat_{stream_date}.jsonl"
    with open(jsonl_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(chunk) + "\n")

    # 2. Embed and store in ChromaDB
    collection = get_collection()
    collection.add(
        documents=[text],
        ids=[chunk_id],
        metadatas=[{
            "stream_date": stream_date,
            "start_time": start_ts,
            "end_time": end_ts,
            "flush_reason": reason,
            "token_count": token_count,
        }],
    )

    # 3. Push to deque for berries_bot short-term memory
    recent_chunks.append(chunk)

    # 4. Keep overlap: drop entries older than CHUNK_OVERLAP_SEC
    cutoff = time.time() - CHUNK_OVERLAP_SEC
    _buffer = [e for e in _buffer if e["timestamp"] >= cutoff]


# ── Background flush timer ─────────────────────────────────────────────────

async def _flush_timer_loop() -> None:
    """Background task: flush buffer on inactivity timeout."""
    while True:
        await asyncio.sleep(10)  # check every 10 seconds
        if _buffer and (time.time() - _last_event_time) >= CHUNK_TIMEOUT_SEC:
            await _flush_buffer(reason="timeout")


@app.on_event("startup")
async def startup() -> None:
    asyncio.create_task(_flush_timer_loop())


# ── Routes ─────────────────────────────────────────────────────────────────

@app.post("/event/chat")
async def receive_chat(
    request: Request,
    x_secret: str | None = Header(default=None),
) -> dict:
    """
    Receive a chat message event from Streamer.bot.

    Expected body:
        {"username": "chatter123", "message": "hello berries!"}
    """
    global _last_event_time

    _auth_check(x_secret)
    body = await request.json()

    username = body.get("username", "unknown")
    raw_text = body.get("message", "")

    cleaned = _preprocess_message(username, raw_text)
    if cleaned is None:
        return {"status": "dropped"}

    _buffer.append({"speaker": username, "text": cleaned, "timestamp": time.time()})
    _last_event_time = time.time()

    # Check token flush condition
    if _buffer_token_count() >= CHUNK_TOKEN_LIMIT:
        await _flush_buffer(reason="token_limit")

    # TODO: fan out to stream_utils for first-time chatter detection
    # TODO: fan out to berries_bot if response is warranted

    return {"status": "ok"}


@app.post("/event/speech")
async def receive_speech(
    request: Request,
    x_secret: str | None = Header(default=None),
) -> dict:
    """
    Receive a speech-to-text transcription event from Streamer.bot.

    Expected body:
        {"text": "so I just hit a really clean hyzer on hole 7"}
    """
    global _last_event_time

    _auth_check(x_secret)
    body = await request.json()

    raw_text = body.get("text", "")
    cleaned = _preprocess_message("StreamerSpeech", raw_text)
    if cleaned is None:
        return {"status": "dropped"}

    _buffer.append({"speaker": "StreamerSpeech", "text": cleaned, "timestamp": time.time()})
    _last_event_time = time.time()

    if _buffer_token_count() >= CHUNK_TOKEN_LIMIT:
        await _flush_buffer(reason="token_limit")

    return {"status": "ok"}


@app.get("/health")
async def health() -> dict:
    return {
        "status": "ok",
        "buffer_entries": len(_buffer),
        "buffer_tokens": _buffer_token_count(),
        "recent_chunks": len(recent_chunks),
    }


# ── MVP: Berries mention trigger ───────────────────────────────────────────

async def _post_to_streamerbot(message: str) -> None:
    """
    POST a chat message to Streamer.bot so it gets sent to Twitch chat.

    Streamer.bot should have an HTTP listener action on port 7474 configured
    to read %request.body.message% and send it as a chat message.
    Adjust the path/payload to match your Streamer.bot action setup.
    """
    payload = {"message": message}
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(STREAMERBOT_CALLBACK_URL, json=payload, timeout=5.0)
            resp.raise_for_status()
        print(f"[ingest_api] Posted to Streamer.bot: {message!r}")
    except Exception as e:
        print(f"[ingest_api] Failed to reach Streamer.bot at {STREAMERBOT_CALLBACK_URL}: {e}")


@app.post("/event/mention")
async def receive_mention(
    request: Request,
    x_secret: str | None = Header(default=None),
) -> dict:
    """
    MVP endpoint: receive a chat message from Streamer.bot, and if it
    contains ':3', post ':3' back to Streamer.bot for Twitch chat.

    Expected body:
        {"text": "hey @BerriesTheDemon :3 lol"}

    Test with curl:
        curl -X POST http://localhost:8000/event/mention \\
             -H "Content-Type: application/json" \\
             -d '{"text": "hello @BerriesTheDemon :3"}'
    """
    _auth_check(x_secret)
    body = await request.json()
    text = body.get("text", "")

    print(f"[ingest_api] /event/mention received: {text!r}")

    if ":3" in text:
        await _post_to_streamerbot(":3")
        return {"status": "ok", "triggered": True, "response": ":3"}

    return {"status": "ok", "triggered": False}
