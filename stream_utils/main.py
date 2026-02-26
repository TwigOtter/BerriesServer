"""
stream_utils/main.py

Stream utility service: first-time chatter detection, and (future) Twitch
predictions. Receives events from ingest_api and responds via Streamer.bot
callbacks.

Run with:
    uvicorn stream_utils.main:app --host 127.0.0.1 --port 8002
"""

import sqlite3
from fastapi import FastAPI, Request

from shared.config import SQLITE_DB_PATH, STREAMERBOT_CALLBACK_URL
import httpx

app = FastAPI(title="Berries Stream Utils")


# ── Database setup ─────────────────────────────────────────────────────────

def _get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(SQLITE_DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """Create tables if they don't exist. Called on startup."""
    with _get_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS known_chatters (
                username TEXT PRIMARY KEY,
                first_seen TEXT NOT NULL
            )
        """)
        conn.commit()


@app.on_event("startup")
async def startup() -> None:
    SQLITE_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    init_db()


# ── First-time chatter logic ───────────────────────────────────────────────

def _is_new_chatter(username: str) -> bool:
    with _get_db() as conn:
        row = conn.execute(
            "SELECT 1 FROM known_chatters WHERE username = ?", (username,)
        ).fetchone()
        return row is None


def _record_chatter(username: str, timestamp: str) -> None:
    with _get_db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO known_chatters (username, first_seen) VALUES (?, ?)",
            (username, timestamp),
        )
        conn.commit()


async def _notify_streamerbot(action: str, payload: dict) -> None:
    """Send a callback to Streamer.bot to trigger a scene/action."""
    if not STREAMERBOT_CALLBACK_URL:
        print(f"[stream_utils] (no callback URL) Action: {action}, payload: {payload}")
        return
    async with httpx.AsyncClient() as client:
        await client.post(
            STREAMERBOT_CALLBACK_URL,
            json={"action": action, **payload},
            timeout=5.0,
        )


# ── Routes ─────────────────────────────────────────────────────────────────

@app.post("/event/chat")
async def handle_chat(request: Request) -> dict:
    """
    Receive a chat event and check for first-time chatters.

    Expected body:
        {"username": "chatter123", "message": "hello!", "timestamp": "2026-02-25T12:00:00Z"}
    """
    body = await request.json()
    username = body.get("username", "")
    timestamp = body.get("timestamp", "")

    if not username:
        return {"status": "error", "detail": "username required"}

    if _is_new_chatter(username):
        _record_chatter(username, timestamp)
        await _notify_streamerbot(
            action="first_time_chatter",
            payload={"username": username},
        )
        return {"status": "ok", "new_chatter": True, "username": username}

    return {"status": "ok", "new_chatter": False}


@app.get("/health")
async def health() -> dict:
    with _get_db() as conn:
        count = conn.execute("SELECT COUNT(*) FROM known_chatters").fetchone()[0]
    return {"status": "ok", "known_chatters": count}


# ── Future: Twitch Predictions ─────────────────────────────────────────────
# TODO: Add endpoints for creating, locking, and resolving Twitch predictions
# Requires OAuth token with channel:manage:predictions scope.
