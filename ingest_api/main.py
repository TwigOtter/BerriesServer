"""
ingest_api/main.py

FastAPI front door. Receives all events from Streamer.bot via HTTP POST,
preprocesses them, manages the chunking buffer, embeds and stores chunks,
and fans out triggers to berries_bot.

Endpoints:
    POST /event/chat          — chat messages (with user subscription data)
    POST /event/speech        — speech-to-text transcription
    POST /event/mention       — response request (triggers Berries to reply)
    POST /event/stream-update — stream title/category change (Streamer.bot update event)
    POST /event/stream        — generic Twitch events (raids, subs, polls, predictions, etc.)
    POST /event/going-live    — stream start (forwarded to Discord bot)
    GET  /health              — status check

Run with:
    uvicorn ingest_api.main:app --host 0.0.0.0 --port 8000
"""

import asyncio
import hmac
import json
import logging
import time
import uuid
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import httpx
from fastapi import FastAPI, Header, HTTPException, Request

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("ingest_api")

from shared.config import (
    CHUNK_OVERLAP_SEC,
    CHUNK_TIMEOUT_SEC,
    CHUNK_TOKEN_LIMIT,
    DISCORD_BOT_WEBHOOK_URL,
    INGEST_SECRET,
    STREAMERBOT_CALLBACK_URL,
    STREAMERBOT_RESPONSE_ACTION_ID,
    TRANSCRIPTS_DIR,
    USERS_DB_PATH,
)
from shared.ask_berries import ask_berries_twitch


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
# Each entry: {"source": str, "text": str, "timestamp": float}
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
    """Reject requests that don't carry the shared secret (if configured).

    Uses hmac.compare_digest for constant-time comparison — protects against
    timing side-channels that could otherwise leak the secret byte-by-byte.
    """
    if not INGEST_SECRET:
        return
    if not hmac.compare_digest(x_secret or "", INGEST_SECRET):
        raise HTTPException(status_code=403, detail="Forbidden")


def _safe_int(value, default: int = 0) -> int:
    """Parse an int from a body field, returning default for missing/empty/unsubstituted values."""
    try:
        return int(value or default)
    except (ValueError, TypeError):
        return default


def _preprocess_message(
    source: str,
    text: str,
    text_stripped: str = "",
    emote_count: int = 0,
) -> str | None:
    """
    Clean a single message before buffering.
    Returns None only if the message is empty after stripping.

    If emote_count > 0 and text_stripped is provided, emote tokens are identified
    by diffing the original message against the stripped version, then consecutive
    repeated emotes are condensed: "PogChamp PogChamp PogChamp" → "PogChamp x3".
    """
    text = text.strip()

    if not text:
        return None

    if emote_count > 0 and text_stripped:
        stripped_words = set(text_stripped.split())
        words = text.split()
        result = []
        i = 0
        while i < len(words):
            word = words[i]
            if word not in stripped_words:
                # Emote token — count consecutive identical repeats
                count = 1
                while i + count < len(words) and words[i + count] == word:
                    count += 1
                result.append(f"{word} x{count}" if count > 1 else word)
                i += count
            else:
                result.append(word)
                i += 1
        text = " ".join(result)

    # TODO: add more noise filters as patterns emerge from real transcripts

    return f"[{source}]: {text}"



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
    sources = list(dict.fromkeys(e["source"] for e in _buffer))  # ordered unique

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
        "source_summary": sources,
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


# ── Routes ─────────────────────────────────────────────────────────────────

# Role values from Streamer.bot: 1=Viewer, 2=VIP, 3=Moderator, 4=Broadcaster
_ROLE_LABELS = {"1": "Viewer", "2": "VIP", "3": "Moderator", "4": "Broadcaster"}


@app.post("/event/chat")
async def receive_chat(
    request: Request,
    x_secret: str | None = Header(default=None),
) -> dict:
    """
    Receive a chat message event from Streamer.bot.

    Expected body:
        {
            "userName": "chatter123",
            "displayName": "Chatter123",
            "userId": "424960237",
            "msgId": "a126e8a8-43f7-4a14-8990-e8c3feea76d8",
            "message": "hello berries! PogChamp PogChamp",
            "messageStripped": "hello berries!",
            "emoteCount": "2",
            "role": "1",
            "bits": "0",
            "firstMessage": "false",
            "isSubscribed": "false",
            "subscriptionTier": "",
            "monthsSubscribed": "0",
            "isVip": "false",
            "isModerator": "false"
        }

    role: "1"=Viewer, "2"=VIP, "3"=Moderator, "4"=Broadcaster
    subscriptionTier: 1000=T1, 2000=T2, 3000=T3 (empty string when not subscribed)
    """
    global _last_event_time

    _auth_check(x_secret)
    body = await request.json()

    username = body.get("userName", "unknown")
    display_name = body.get("displayName") or username
    user_id = body.get("userId", "")
    msg_id = body.get("msgId", "")
    raw_text = body.get("message", "")
    text_stripped = body.get("messageStripped", "")
    emote_count = _safe_int(body.get("emoteCount"))
    role = str(body.get("role", "1"))
    role_label = _ROLE_LABELS.get(role, "Viewer")
    bits = _safe_int(body.get("bits"))
    first_message = str(body.get("firstMessage", "false")).lower() == "true"
    is_subscribed = str(body.get("isSubscribed", "false")).lower() == "true"
    is_vip = str(body.get("isVip", "false")).lower() == "true"
    is_moderator = str(body.get("isModerator", "false")).lower() == "true"

    # subscriptionTier is 1000/2000/3000 from Streamer.bot; only present when subscribed
    sub_tier = {1000: 1, 2000: 2, 3000: 3}.get(_safe_int(body.get("subscriptionTier")), 0)
    sub_months = _safe_int(body.get("monthsSubscribed"))

    # msg_id retained for future direct-reply support
    flags = [role_label]
    if is_subscribed:
        flags.append(f"sub T{sub_tier}/{sub_months}mo")
    if is_vip:
        flags.append("VIP")
    if is_moderator:
        flags.append("mod")
    if first_message:
        flags.append("first!")
    if bits:
        flags.append(f"{bits} bits")
    logger.info("/event/chat — %s (%s) [%s]: %r", display_name, user_id, ", ".join(flags), raw_text)

    # Always upsert user profile, even if the message itself gets dropped.
    # This ensures rename tracking fires on every chat event regardless of content.
    try:
        parsed_user_id = int(user_id) if user_id else None
    except (ValueError, TypeError):
        parsed_user_id = None
    from shared.user_db import upsert_user
    upsert_user(
        t_login=username,
        t_display_name=display_name,
        t_subscription_tier=sub_tier,
        t_subscription_months=sub_months,
        t_id=parsed_user_id,
    )

    cleaned = _preprocess_message(display_name, raw_text, text_stripped, emote_count)
    if cleaned is None:
        return {"status": "dropped"}

    _buffer.append({"source": display_name, "text": cleaned, "timestamp": time.time()})
    _last_event_time = time.time()
    _session_chatters.add(display_name)

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

    _buffer.append({"source": body.get("speaker", "Unknown"), "text": cleaned, "timestamp": time.time()})
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

    logger.info("Stream metadata updated: %s", _stream_metadata)
    return {"status": "ok", "stream_metadata": _stream_metadata}


@app.post("/event/stream")
async def receive_stream_event(
    request: Request,
    x_secret: str | None = Header(default=None),
) -> dict:
    """
    Receive a generic Twitch event from Streamer.bot.
    Streamer.bot pre-formats the human-readable description — this endpoint
    just buffers it alongside chat and speech.

    Expected body:
        {"type": "subscription", "text": "viewer123 just subscribed at Tier 1 for 3 months!"}
        {"type": "raid", "text": "SomeStreamer raided with 42 viewers!"}
        {"type": "prediction", "text": "Prediction started: 'Will Twig beat this level?' Yes | No"}
    """
    global _last_event_time

    _auth_check(x_secret)
    body = await request.json()

    event_type = body.get("type", "stream_event")
    text = body.get("text", "").strip()

    if not text:
        return {"status": "dropped", "reason": "empty text"}

    line = f"[StreamEvent]: {text}"
    _buffer.append({"source": "StreamEvent", "text": line, "timestamp": time.time()})
    _last_event_time = time.time()

    if _buffer_token_count() >= CHUNK_TOKEN_LIMIT:
        await _flush_buffer(reason="token_limit")

    logger.info("/event/stream (%s) — %s", event_type, line)
    return {"status": "ok"}


@app.post("/event/going-live")
async def going_live(
    request: Request,
    x_secret: str | None = Header(default=None),
) -> dict:
    """
    Receive a going-live event from Streamer.bot and forward to the Discord bot webhook.

    Expected body:
        {"title": "Stream title here", "category": "Games & Demos"}
    """
    _auth_check(x_secret)
    body = await request.json()

    logger.info("/event/going-live — %s", body)

    try:
        async with httpx.AsyncClient() as client:
            await client.post(
                f"{DISCORD_BOT_WEBHOOK_URL}/event/going-live",
                json=body,
                headers={"X-Secret": INGEST_SECRET} if INGEST_SECRET else {},
                timeout=10.0,
            )
    except Exception as e:
        logger.warning("Failed to forward going-live to discord_bot: %s", e)

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



async def _post_to_streamerbot(
    message: str,
    chat: bool = False,
    tts: bool = False,
) -> None:
    """
    POST Berries' response back to Streamer.bot at the URL/action configured in .env.
    Streamer.bot reads %request.body.message%, %request.body.CHAT%, %request.body.TTS%
    and uses them to decide which actions to trigger.

    The destination URL and action ID are pinned to STREAMERBOT_CALLBACK_URL /
    STREAMERBOT_RESPONSE_ACTION_ID from config — never taken from request bodies.
    """
    payload = {
        "action": {
            "id": STREAMERBOT_RESPONSE_ACTION_ID
        },
        "args": {
            "message": message,
            "CHAT": chat,
            "TTS": tts
        }
    }
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(STREAMERBOT_CALLBACK_URL, json=payload, timeout=5.0)
            resp.raise_for_status()
        logger.info("Posted to Streamer.bot: %r (CHAT=%s, TTS=%s)", message, chat, tts)
    except Exception as e:
        logger.warning("Failed to reach Streamer.bot at %s: %s", STREAMERBOT_CALLBACK_URL, e)


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
            "text": "Hey Berries, what's Twig's favorite game?",
            "username": "the_detective",
            "CHAT": false,
            "TTS": false,
            "log": true
        }

    The response is always posted back to STREAMERBOT_CALLBACK_URL using
    STREAMERBOT_RESPONSE_ACTION_ID from .env — neither can be overridden by the
    request body (prevents SSRF / response hijack if INGEST_SECRET ever leaks).

    Test with:
        Invoke-RestMethod -Uri "http://localhost:8000/event/mention" -Method POST `
            -ContentType "application/json" `
            -Body '{"text": "hey Berries, what do you think about mushrooms?", "CHAT": true, "TTS": false}'

    TODO: when log=false, skip writing text to transcript and ChromaDB.
    """
    _auth_check(x_secret)
    body = await request.json()

    text = body.get("text", "")
    username = body.get("username", "")
    chat = body.get("CHAT", False)
    tts = body.get("TTS", False)
    # log = body.get("log", True)  # TODO: use to suppress transcript writes

    logger.info("/event/mention — username=%r text=%r CHAT=%s TTS=%s", username, text, chat, tts)

    if not text:
        return {"status": "ok", "triggered": False}

    response_text = await ask_berries_twitch(
        query=text,
        username=username,
        tts=tts,
        recent_chunks=list(recent_chunks),
        recent_buffer_text="\n".join(e["text"] for e in _buffer[-15:]),
    )
    await _post_to_streamerbot(response_text, chat=chat, tts=tts)

    return {
        "message": response_text,
        "CHAT": chat,
        "TTS": tts,
    }
