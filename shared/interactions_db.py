"""
shared/interactions_db.py

Per-event interaction store — Phase 1 of docs/sql-interaction-storage.md.

SQLite tables of raw platform events (twitch_events, discord_messages),
dual-written alongside the existing JSONL/Chroma flow. The endgame is for
this DB to become the system of record that ChromaDB chunks are derived
from; in Phase 1 nothing reads it yet.

Conventions (matching shared/user_db.py): module of functions, `_connect()`,
`init_db()` with incremental migrations. Differences, both deliberate:

- WAL mode + busy timeout: ingest_api and discord_bot write concurrently
  from separate processes.
- Writers swallow exceptions (loudly): dual-write is an addition to the
  response/ingest paths, and a logging failure must never break them.
  Revisit when something starts *reading* this DB (Phase 2).

Timestamps: `created_at` is a UTC ISO instant; `stream_date` is a local
calendar day (LOCAL_TIMEZONE) — same convention as the daily logs.
"""

import json
import logging
import sqlite3
from datetime import datetime, timezone

from shared.config import INTERACTIONS_DB_PATH, LOCAL_TZ

log = logging.getLogger(__name__)


def _connect() -> sqlite3.Connection:
    INTERACTIONS_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(INTERACTIONS_DB_PATH), timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


_CREATE_TWITCH = """
    CREATE TABLE IF NOT EXISTS twitch_events (
        id              INTEGER PRIMARY KEY,
        created_at      TEXT NOT NULL,
        stream_date     TEXT NOT NULL,
        stream_title    TEXT,
        stream_category TEXT,
        user_id         INTEGER,
        username        TEXT,
        display_name    TEXT,
        type            TEXT NOT NULL,
        content         TEXT,
        payload         TEXT,
        message_id      TEXT UNIQUE,
        reply_to_message_id TEXT,
        is_bot          INTEGER NOT NULL DEFAULT 0,
        invoked_berries INTEGER NOT NULL DEFAULT 0
    )
"""

_CREATE_DISCORD = """
    CREATE TABLE IF NOT EXISTS discord_messages (
        id                  INTEGER PRIMARY KEY,
        created_at          TEXT NOT NULL,
        guild_id            TEXT,
        channel_id          TEXT NOT NULL,
        channel_name        TEXT,
        user_id             TEXT NOT NULL,
        username            TEXT,
        display_name        TEXT,
        message_id          TEXT UNIQUE,
        message_text        TEXT,
        reply_to_message_id TEXT,
        is_bot              INTEGER NOT NULL DEFAULT 0,
        invoked_berries     INTEGER NOT NULL DEFAULT 0
    )
"""

_CREATE_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_twitch_user_time ON twitch_events(user_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_twitch_time      ON twitch_events(created_at)",
    "CREATE INDEX IF NOT EXISTS idx_discord_user_time    ON discord_messages(user_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_discord_channel_time ON discord_messages(channel_id, created_at)",
]


def init_db() -> None:
    """Create tables and indexes if they don't exist. Safe to call repeatedly."""
    with _connect() as conn:
        conn.execute(_CREATE_TWITCH)
        conn.execute(_CREATE_DISCORD)
        for stmt in _CREATE_INDEXES:
            conn.execute(stmt)


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _today_local() -> str:
    return datetime.now(LOCAL_TZ).strftime("%Y-%m-%d")


def log_twitch_event(
    *,
    type: str,
    content: str | None = None,
    user_id: int | None = None,
    username: str | None = None,
    display_name: str | None = None,
    stream_title: str | None = None,
    stream_category: str | None = None,
    payload: dict | None = None,
    message_id: str | None = None,
    reply_to_message_id: str | None = None,
    is_bot: bool = False,
    invoked_berries: bool = False,
) -> None:
    """
    Record one Twitch event. Best-effort: exceptions are logged, never raised.

    Duplicate `message_id`s are ignored (Streamer.bot may retry deliveries).
    """
    try:
        with _connect() as conn:
            conn.execute(
                """
                INSERT INTO twitch_events (
                    created_at, stream_date, stream_title, stream_category,
                    user_id, username, display_name, type, content, payload,
                    message_id, reply_to_message_id, is_bot, invoked_berries
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(message_id) DO NOTHING
                """,
                (
                    _now_utc(), _today_local(), stream_title, stream_category,
                    user_id, username, display_name, type, content,
                    json.dumps(payload) if payload else None,
                    message_id or None, reply_to_message_id or None,
                    int(is_bot), int(invoked_berries),
                ),
            )
    except Exception:
        log.exception("Failed to record twitch event (type=%s)", type)


def log_discord_message(
    *,
    channel_id: str,
    user_id: str,
    message_text: str | None = None,
    guild_id: str | None = None,
    channel_name: str | None = None,
    username: str | None = None,
    display_name: str | None = None,
    message_id: str | None = None,
    reply_to_message_id: str | None = None,
    is_bot: bool = False,
    invoked_berries: bool = False,
    created_at: str | None = None,
) -> None:
    """
    Record one Discord message. Best-effort: exceptions are logged, never raised.

    A message can arrive through both the watcher cog and the mention cog
    (watched channel + @mention). `message_id` is UNIQUE; on conflict the
    row is kept and only `invoked_berries` is escalated, so whichever cog
    writes second cannot downgrade the flag.

    created_at: pass the message's own UTC ISO timestamp when known
    (discord.Message.created_at); defaults to now.
    """
    try:
        with _connect() as conn:
            conn.execute(
                """
                INSERT INTO discord_messages (
                    created_at, guild_id, channel_id, channel_name,
                    user_id, username, display_name, message_id, message_text,
                    reply_to_message_id, is_bot, invoked_berries
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(message_id) DO UPDATE SET
                    invoked_berries = MAX(invoked_berries, excluded.invoked_berries)
                """,
                (
                    created_at or _now_utc(), guild_id, channel_id, channel_name,
                    user_id, username, display_name, message_id or None, message_text,
                    reply_to_message_id or None, int(is_bot), int(invoked_berries),
                ),
            )
    except Exception:
        log.exception("Failed to record discord message (channel=%s)", channel_id)
