"""
ingest_api/main.py

FastAPI front door. Receives all events from Streamer.bot via HTTP POST,
preprocesses them, manages the chunking buffer, embeds and stores chunks,
and fans out triggers to berries_bot.

Endpoints:
    POST /event/chat         — chat messages (with user subscription data)
    POST /event/speech       — speech-to-text transcription
    POST /event/mention      — response request (triggers Berries to reply)
    POST /event/stream-update — stream title/category change (Streamer.bot update event)
    POST /event/stream       — generic Twitch events (raids, subs, polls, predictions, etc.)
    GET  /health             — status check

Run with:
    uvicorn ingest_api.main:app --host 0.0.0.0 --port 8000
"""

import asyncio
import json
import time
import uuid
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import httpx
from fastapi import FastAPI, Header, HTTPException, Request

from shared.config import (
    CHUNK_OVERLAP_SEC,
    CHUNK_TIMEOUT_SEC,
    CHUNK_TOKEN_LIMIT,
    CHROMA_N_RESULTS,
    INGEST_SECRET,
    PERSONALITY_FILE,
    STREAMERBOT_CALLBACK_URL,
    TRANSCRIPTS_DIR,
    USERS_DB_PATH,
)


def get_collection():
    from shared.chroma_client import get_collection as _get_collection
    return _get_collection()


def count_tokens(text: str) -> int:
    from shared.tokenizer import count_tokens as _count_tokens
    return _count_tokens(text)


@asynccontextmanager
async def lifespan(_app):
    from shared.user_db import init_db
    USERS_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    init_db()
    asyncio.create_task(_flush_timer_loop())
    yield


app = FastAPI(title="Berries Ingest API", lifespan=lifespan)

# ── In-memory state ────────────────────────────────────────────────────────
# Each entry: {"speaker": str, "text": str, "timestamp": float}
_buffer: list[dict] = []
_last_event_time: float = time.time()

# Shared deque for recent chunks — used for short-term memory in response generation
recent_chunks: deque = deque(maxlen=2)

# Tracks all usernames who chatted this session (for streams_watched rollup at stream end)
_session_chatters: set[str] = set()

# Current stream metadata — updated by /event/stream-update
_stream_metadata: dict = {"title": "", "category": ""}


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


def _format_stream_event(event_type: str, body: dict) -> str | None:
    """
    Convert a generic Twitch event into a human-readable line for the buffer.
    Returns None if the event type is unknown/unhandled.
    """
    match event_type:
        case "subscription":
            user = body.get("username", "Someone")
            tier = body.get("tier", 1)
            months = body.get("months", 1)
            return f"[StreamEvent]: {user} subscribed at Tier {tier} for {months} month(s)!"
        case "gift_sub":
            gifter = body.get("gifter", "Anonymous")
            recipient = body.get("recipient", "someone")
            tier = body.get("tier", 1)
            return f"[StreamEvent]: {gifter} gifted a Tier {tier} sub to {recipient}!"
        case "bits":
            user = body.get("username", "Someone")
            amount = body.get("amount", 0)
            return f"[StreamEvent]: {user} cheered {amount} bits!"
        case "raid":
            channel = body.get("from_channel", "someone")
            count = body.get("viewer_count", 0)
            return f"[StreamEvent]: {channel} raided with {count} viewer(s)!"
        case "first_time_chatter":
            user = body.get("username", "someone")
            return f"[StreamEvent]: {user} chatted for the first time!"
        case "prediction_start":
            title = body.get("title", "?")
            outcomes = body.get("outcomes", [])
            opts = " / ".join(outcomes)
            return f"[StreamEvent]: Prediction started — \"{title}\" ({opts})"
        case "prediction_lock":
            title = body.get("title", "?")
            return f"[StreamEvent]: Prediction locked — \"{title}\""
        case "prediction_result":
            title = body.get("title", "?")
            winner = body.get("winner", "?")
            points = body.get("total_points", 0)
            return f"[StreamEvent]: Prediction ended — \"{title}\" — winner: {winner} ({points:,} points wagered)"
        case "poll_start":
            title = body.get("title", "?")
            choices = body.get("choices", [])
            opts = " / ".join(choices)
            return f"[StreamEvent]: Poll started — \"{title}\" ({opts})"
        case "poll_end":
            title = body.get("title", "?")
            winner = body.get("winner", "?")
            return f"[StreamEvent]: Poll ended — \"{title}\" — winner: {winner}"
        case _:
            return None


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
        "stream_title": _stream_metadata["title"],
        "stream_category": _stream_metadata["category"],
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
            "stream_title": _stream_metadata["title"],
            "stream_category": _stream_metadata["category"],
            "start_time": start_ts,
            "end_time": end_ts,
            "flush_reason": reason,
            "token_count": token_count,
        }],
    )

    # 3. Push to deque for short-term memory
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
    from shared.user_db import init_db
    USERS_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    init_db()
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
        {
            "username": "chatter123",
            "display_name": "Chatter123",
            "message": "hello berries!",
            "subscription_tier": 1,
            "subscription_months": 3,
            "gift_sub_count": 0,
            "is_moderator": false
        }

    subscription_tier: 0=none, 1=Tier1, 2=Tier2, 3=Tier3
    """
    global _last_event_time

    _auth_check(x_secret)
    body = await request.json()

    username = body.get("username", "unknown")
    display_name = body.get("display_name") or username
    raw_text = body.get("message", "")
    sub_tier = int(body.get("subscription_tier", 0))
    sub_months = int(body.get("subscription_months", 0))
    gift_subs = int(body.get("gift_sub_count", 0))

    cleaned = _preprocess_message(username, raw_text)
    if cleaned is None:
        return {"status": "dropped"}

    _buffer.append({"speaker": username, "text": cleaned, "timestamp": time.time()})
    _last_event_time = time.time()
    _session_chatters.add(username)

    # Upsert user profile passively from chat event data
    from shared.user_db import upsert_user
    upsert_user(
        username=username,
        display_name=display_name,
        subscription_tier=sub_tier,
        subscription_months=sub_months,
        gift_sub_count=gift_subs,
    )

    if _buffer_token_count() >= CHUNK_TOKEN_LIMIT:
        await _flush_buffer(reason="token_limit")

    return {"status": "ok"}


@app.post("/event/speech")
async def receive_speech(
    request: Request,
    x_secret: str | None = Header(default=None),
) -> dict:
    """
    Receive a speech-to-text transcription event from Streamer.bot.

    Expected body:
        {"speaker": "TwigOtter", "text": "Welcome on in everyone thank you so much for joining!"}
    """
    global _last_event_time

    _auth_check(x_secret)
    body = await request.json()

    raw_text = body.get("text", "")
    cleaned = _preprocess_message(body.get("speaker", "Unknown"), raw_text)
    if cleaned is None:
        return {"status": "dropped"}

    _buffer.append({"speaker": body.get("speaker", "Unknown"), "text": cleaned, "timestamp": time.time()})
    _last_event_time = time.time()

    if _buffer_token_count() >= CHUNK_TOKEN_LIMIT:
        await _flush_buffer(reason="token_limit")

    return {"status": "ok"}


@app.post("/event/stream-update")
async def receive_stream_update(
    request: Request,
    x_secret: str | None = Header(default=None),
) -> dict:
    """
    Receive a stream metadata update from Streamer.bot.
    Streamer.bot fires this on its built-in "Stream Update" event whenever
    the title or category changes. Applied to all subsequent chunks.

    Expected body:
        {"title": "Cozy Chaos with a Silly Otter", "category": "Games & Demos"}
    """
    _auth_check(x_secret)
    body = await request.json()

    _stream_metadata["title"] = body.get("title", "")
    _stream_metadata["category"] = body.get("category", "")

    print(f"[ingest_api] Stream metadata updated: {_stream_metadata}")
    return {"status": "ok", "stream_metadata": _stream_metadata}


@app.post("/event/stream")
async def receive_stream_event(
    request: Request,
    x_secret: str | None = Header(default=None),
) -> dict:
    """
    Receive a generic Twitch event from Streamer.bot.
    Raids, subscriptions, gift subs, bits, predictions, polls, first-time
    chatters, and any other event Streamer.bot can capture all flow here.

    Expected body: {"type": "<event_type>", ...event-specific fields}

    Supported types: subscription, gift_sub, bits, raid, first_time_chatter,
                     prediction_start, prediction_lock, prediction_result,
                     poll_start, poll_end
    """
    global _last_event_time

    _auth_check(x_secret)
    body = await request.json()

    event_type = body.get("type", "")
    line = _format_stream_event(event_type, body)

    if line is None:
        print(f"[ingest_api] /event/stream — unhandled type: {event_type!r}")
        return {"status": "unhandled", "type": event_type}

    _buffer.append({"speaker": "StreamEvent", "text": line, "timestamp": time.time()})
    _last_event_time = time.time()

    if _buffer_token_count() >= CHUNK_TOKEN_LIMIT:
        await _flush_buffer(reason="token_limit")

    print(f"[ingest_api] /event/stream — {line}")
    return {"status": "ok"}


@app.get("/health")
async def health() -> dict:
    return {
        "status": "ok",
        "buffer_entries": len(_buffer),
        "buffer_tokens": _buffer_token_count(),
        "recent_chunks": len(recent_chunks),
        "stream_metadata": _stream_metadata,
        "session_chatters": len(_session_chatters),
    }


# ── Berries response pipeline ──────────────────────────────────────────────

def _load_personality() -> str:
    """Load Berries' system prompt from berries_bot/personality.txt."""
    if PERSONALITY_FILE.exists():
        return PERSONALITY_FILE.read_text(encoding="utf-8").strip()
    print("[ingest_api] WARNING: personality.txt not found, using fallback prompt.")
    return "You are Berries, a spooky and playful forest demon on a Twitch stream. Keep responses short and in character."


async def _generate_response(text: str) -> str:
    """
    Build context and call the LLM to generate Berries' response.
    Injects short-term memory (recent deque chunks) and long-term memory
    (semantically relevant past chunks from ChromaDB) into the system prompt.
    """
    from shared.llm_client import get_completion

    system_prompt = _load_personality()

    # Long-term memory: semantically relevant past chunks
    try:
        collection = get_collection()
        results = collection.query(query_texts=[text], n_results=CHROMA_N_RESULTS)
        docs = results.get("documents", [[]])[0]
        if docs:
            context_block = "\n---\n".join(docs)
            system_prompt += f"\n\nRELEVANT PAST CONTEXT:\n{context_block}"
    except Exception as e:
        print(f"[ingest_api] ChromaDB query failed (no context injected): {e}")

    # Short-term memory: last 2 chunks from current session
    if recent_chunks:
        recent_text = "\n---\n".join(c["text"] for c in recent_chunks)
        system_prompt += f"\n\nRECENT CONVERSATION:\n{recent_text}"

    return await get_completion(system_prompt=system_prompt, user_message=text)


async def _post_to_streamerbot(message: str, post_to_chat: bool = True, tts: bool = False) -> None:
    """
    POST Berries' response back to Streamer.bot.
    Streamer.bot reads %request.body.message%, %request.body.postToChat%, %request.body.TTS%.

    TODO: Route TTS responses to SpeakerBot via a separate Streamer.bot action
          once TTS pipeline is configured.
    """
    payload = {
        "message": message,
        "postToChat": post_to_chat,
        "TTS": tts,
    }
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(STREAMERBOT_CALLBACK_URL, json=payload, timeout=5.0)
            resp.raise_for_status()
        print(f"[ingest_api] Posted to Streamer.bot: {message!r} (TTS={tts})")
    except Exception as e:
        print(f"[ingest_api] Failed to reach Streamer.bot at {STREAMERBOT_CALLBACK_URL}: {e}")


@app.post("/event/mention")
async def receive_mention(
    request: Request,
    x_secret: str | None = Header(default=None),
) -> dict:
    """
    Receive a response request from Streamer.bot, generate a Berries response
    via LLM, and post it back.

    Expected body:
        {
            "text": "[SomeUsername] Hey Berries, what's Twig's favorite game?",
            "respond": true,
            "TTS": false,
            "log": true
        }

    Test with:
        Invoke-RestMethod -Uri "http://localhost:8000/event/mention" -Method POST `
            -ContentType "application/json" `
            -Body '{"text": "[ChatUser] hey Berries, what do you think about mushrooms?", "respond": true, "TTS": false}'

    TODO: when log=false, skip writing text to transcript and ChromaDB.
    TODO: per-user context injection from users.db (sub tier, nickname, follow date, etc.)
    """
    _auth_check(x_secret)
    body = await request.json()

    text = body.get("text", "")
    should_respond = body.get("respond", True)
    tts = body.get("TTS", False)
    # log = body.get("log", True)  # TODO: use to suppress transcript writes

    print(f"[ingest_api] /event/mention — text={text!r} respond={should_respond} TTS={tts}")

    if not should_respond or not text:
        return {"status": "ok", "triggered": False}

    response_text = await _generate_response(text)
    await _post_to_streamerbot(response_text, post_to_chat=True, tts=tts)

    return {
        "message": response_text,
        "postToChat": True,
        "TTS": tts,
    }
