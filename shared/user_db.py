"""
shared/user_db.py

SQLite-backed user profile store. All services that need per-user data import
from here. The DB is created automatically on first use.

Schema:
    users — one row per chatter, updated passively from Streamer.bot chat events.
"""

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from shared.config import USERS_DB_PATH


def _connect() -> sqlite3.Connection:
    USERS_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(USERS_DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """Create tables if they don't exist. Call once at service startup."""
    with _connect() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                username            TEXT PRIMARY KEY,
                display_name        TEXT,
                nickname            TEXT,
                subscription_tier   INTEGER NOT NULL DEFAULT 0,
                subscription_months INTEGER NOT NULL DEFAULT 0,
                gift_sub_count      INTEGER NOT NULL DEFAULT 0,
                messages_sent       INTEGER NOT NULL DEFAULT 0,
                streams_watched     INTEGER NOT NULL DEFAULT 0,
                notes               TEXT NOT NULL DEFAULT '{}',
                first_seen          TEXT NOT NULL,
                last_seen           TEXT NOT NULL
            )
        """)
        conn.commit()
        # Migration: add discord_id column if it doesn't already exist
        try:
            conn.execute("ALTER TABLE users ADD COLUMN discord_id TEXT")
            conn.commit()
        except sqlite3.OperationalError:
            pass  # Column already exists


def upsert_user(
    username: str,
    display_name: str | None = None,
    subscription_tier: int = 0,
    subscription_months: int = 0,
    gift_sub_count: int = 0,
    timestamp: str | None = None,
) -> None:
    """
    Insert a new user or update an existing one on every chat event.
    Increments messages_sent. Subscription data is always overwritten with the
    latest values from Streamer.bot (source of truth).
    """
    if timestamp is None:
        timestamp = datetime.now(timezone.utc).isoformat()

    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO users (
                username, display_name, subscription_tier, subscription_months,
                gift_sub_count, messages_sent, first_seen, last_seen
            )
            VALUES (?, ?, ?, ?, ?, 1, ?, ?)
            ON CONFLICT(username) DO UPDATE SET
                display_name        = COALESCE(excluded.display_name, display_name),
                subscription_tier   = excluded.subscription_tier,
                subscription_months = excluded.subscription_months,
                gift_sub_count      = excluded.gift_sub_count,
                messages_sent       = messages_sent + 1,
                last_seen           = excluded.last_seen
            """,
            (
                username,
                display_name,
                subscription_tier,
                subscription_months,
                gift_sub_count,
                timestamp,
                timestamp,
            ),
        )
        conn.commit()


def get_user(username: str) -> dict | None:
    """Return a user's full profile as a dict, or None if not found."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE username = ?", (username,)
        ).fetchone()
    if row is None:
        return None
    result = dict(row)
    result["notes"] = json.loads(result["notes"])
    return result


def set_nickname(username: str, nickname: str) -> None:
    """Set or update a user's nickname (used organically by Berries)."""
    with _connect() as conn:
        conn.execute(
            "UPDATE users SET nickname = ? WHERE username = ?",
            (nickname, username),
        )
        conn.commit()


def add_note(username: str, key: str, value: str) -> None:
    """
    Merge a key/value pair into a user's JSON notes field.
    Creates the user row first if it doesn't exist.
    """
    now = datetime.now(timezone.utc).isoformat()
    with _connect() as conn:
        row = conn.execute(
            "SELECT notes FROM users WHERE username = ?", (username,)
        ).fetchone()
        if row is None:
            conn.execute(
                """
                INSERT INTO users (username, notes, first_seen, last_seen)
                VALUES (?, ?, ?, ?)
                """,
                (username, json.dumps({key: value}), now, now),
            )
        else:
            notes = json.loads(row["notes"])
            notes[key] = value
            conn.execute(
                "UPDATE users SET notes = ? WHERE username = ?",
                (json.dumps(notes), username),
            )
        conn.commit()


def increment_streams_watched(usernames: list[str]) -> None:
    """
    Increment streams_watched for all usernames in the list.
    Call once per stream end for everyone who chatted that session.
    """
    if not usernames:
        return
    with _connect() as conn:
        conn.executemany(
            "UPDATE users SET streams_watched = streams_watched + 1 WHERE username = ?",
            [(u,) for u in usernames],
        )
        conn.commit()


def link_discord(twitch_username: str, discord_id: str) -> dict:
    """
    Associate a Discord user ID with a Twitch username.

    Creates a minimal user row if the Twitch username doesn't exist yet.
    If the Discord ID was previously linked to a different Twitch account,
    that old link is cleared first.

    Returns a dict with:
      status          — "already_linked" | "linked" | "updated"
      twitch_username — the (normalised) Twitch username now stored
      previous        — the old Twitch username if the Discord ID moved, else None
    """
    twitch_username = twitch_username.lower().strip()
    now = datetime.now(timezone.utc).isoformat()

    with _connect() as conn:
        # Check if this Discord ID is already linked somewhere
        existing = conn.execute(
            "SELECT username FROM users WHERE discord_id = ?", (discord_id,)
        ).fetchone()

        if existing and existing["username"] == twitch_username:
            return {"status": "already_linked", "twitch_username": twitch_username, "previous": None}

        # Clear the old discord_id so one Discord account maps to one Twitch account
        if existing:
            conn.execute(
                "UPDATE users SET discord_id = NULL WHERE discord_id = ?", (discord_id,)
            )

        # Upsert the target Twitch user row
        target = conn.execute(
            "SELECT username FROM users WHERE username = ?", (twitch_username,)
        ).fetchone()

        if target:
            conn.execute(
                "UPDATE users SET discord_id = ? WHERE username = ?",
                (discord_id, twitch_username),
            )
            status = "updated"
        else:
            conn.execute(
                """
                INSERT INTO users (username, discord_id, notes, first_seen, last_seen)
                VALUES (?, ?, '{}', ?, ?)
                """,
                (twitch_username, discord_id, now, now),
            )
            status = "linked"

        conn.commit()

    return {
        "status": status,
        "twitch_username": twitch_username,
        "previous": existing["username"] if existing else None,
    }


def get_twitch_link(discord_id: str) -> str | None:
    """Return the Twitch username linked to a Discord user ID, or None."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT username FROM users WHERE discord_id = ?", (discord_id,)
        ).fetchone()
    return row["username"] if row else None


def get_discord_for_twitch(twitch_username: str) -> str | None:
    """Return the Discord user ID linked to a Twitch username, or None."""
    twitch_username = twitch_username.lower().strip()
    with _connect() as conn:
        row = conn.execute(
            "SELECT discord_id FROM users WHERE username = ?", (twitch_username,)
        ).fetchone()
    return row["discord_id"] if row else None
