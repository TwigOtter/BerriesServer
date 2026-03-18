"""
shared/user_db.py

SQLite-backed user profile store. All services that need per-user data import
from here. The DB is created automatically on first use.

Column prefixes
---------------
  t_   Twitch-specific data (login, sub stats, activity counts, etc.)
  d_   Discord-specific data (snowflake ID, username history, etc.)
  (none) Platform-agnostic profile fields and record meta.

Schema
------
  id                    TEXT  PK       Our stable internal UUID; never changes.
  t_id                  INT           Twitch numeric user ID — stable platform key.
  t_login               TEXT  UNIQUE  Lowercase Twitch login, e.g. "twigotter".
  t_display_name        TEXT          Case-preserved Twitch name, e.g. "TwigOtter".
  t_past_logins         TEXT          JSON list of previous Twitch login names.
  t_subscription_tier   INT           0 = none, 1/2/3 = tier.
  t_subscription_months INT           Cumulative months subscribed.
  t_gift_sub_count      INT
  t_messages_sent       INT
  t_streams_watched     INT
  d_id                  TEXT  UNIQUE  Discord snowflake ID — stable platform key.
  d_username            TEXT          Current Discord username, e.g. "twigotter".
  d_past_usernames      TEXT          JSON list of previous Discord usernames.
  nickname              TEXT          What Berries calls them (cross-platform).
  pronouns              TEXT          e.g. "she/her", "they/them".
  species               TEXT          e.g. "red fox", "border collie".
  timezone              TEXT          IANA format, e.g. "America/New_York".
  birthday              TEXT          MM-DD only, no year.
  country               TEXT
  notes                 TEXT          JSON blob for Berries' ad-hoc observations.
  first_seen            TEXT          ISO timestamp — when record was created.
  last_seen             TEXT          ISO timestamp — most recent activity.
"""

import json
import sqlite3
import uuid
from datetime import datetime, timezone

from shared.config import USERS_DB_PATH


def _connect() -> sqlite3.Connection:
    USERS_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(USERS_DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


_CREATE_TABLE = """
    CREATE TABLE IF NOT EXISTS users (
        id                    TEXT PRIMARY KEY,
        t_id                  INTEGER,
        t_login               TEXT,
        t_display_name        TEXT,
        t_past_logins         TEXT NOT NULL DEFAULT '[]',
        t_subscription_tier   INTEGER NOT NULL DEFAULT 0,
        t_subscription_months INTEGER NOT NULL DEFAULT 0,
        t_gift_sub_count      INTEGER NOT NULL DEFAULT 0,
        t_messages_sent       INTEGER NOT NULL DEFAULT 0,
        t_streams_watched     INTEGER NOT NULL DEFAULT 0,
        d_id                  TEXT,
        d_username            TEXT,
        d_past_usernames      TEXT NOT NULL DEFAULT '[]',
        nickname              TEXT,
        pronouns              TEXT,
        species               TEXT,
        timezone              TEXT,
        birthday              TEXT,
        country               TEXT,
        notes                 TEXT NOT NULL DEFAULT '{}',
        first_seen            TEXT NOT NULL,
        last_seen             TEXT NOT NULL,
        CHECK (t_login IS NOT NULL OR d_id IS NOT NULL)
    )
"""


def _maybe_migrate(conn: sqlite3.Connection) -> None:
    """
    If the users table exists with the old NOT NULL t_login constraint, migrate
    it to the new schema (nullable t_login, CHECK that t_login or d_id is set).
    Uses the standard SQLite rename-recreate-copy pattern.
    """
    tables = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='users'"
    ).fetchone()
    if not tables:
        return
    col_info = {row["name"]: row for row in conn.execute("PRAGMA table_info(users)").fetchall()}
    if col_info.get("t_login", {})["notnull"]:
        conn.execute("ALTER TABLE users RENAME TO users_old")
        conn.execute(_CREATE_TABLE)
        conn.execute("INSERT INTO users SELECT * FROM users_old")
        conn.execute("DROP TABLE users_old")


def init_db() -> None:
    """Create tables and indexes if they don't exist. Call once at service startup."""
    with _connect() as conn:
        _maybe_migrate(conn)
        conn.execute(_CREATE_TABLE)
        # Partial unique indexes allow multiple NULLs while enforcing uniqueness
        # for rows that do have a value.
        conn.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_users_t_id
            ON users(t_id) WHERE t_id IS NOT NULL
        """)
        conn.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_users_d_id
            ON users(d_id) WHERE d_id IS NOT NULL
        """)
        conn.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_users_t_login
            ON users(t_login) WHERE t_login IS NOT NULL
        """)
        conn.commit()


def upsert_user(
    t_login: str,
    t_display_name: str | None = None,
    t_subscription_tier: int = 0,
    t_subscription_months: int = 0,
    t_gift_sub_count: int = 0,
    timestamp: str | None = None,
    t_id: int | None = None,
) -> None:
    """
    Insert a new user or update an existing one on every chat event.
    Increments t_messages_sent. Subscription data is always overwritten with
    the latest values from Streamer.bot (source of truth).

    When t_id is provided it is used as the stable lookup key. If an existing
    row is found under that t_id with a different t_login, the old login is
    appended to t_past_logins and t_login is updated in place — this handles
    Twitch name changes transparently while preserving all stats.

    For rows that pre-date t_id tracking (t_login match, no t_id set),
    the t_id is back-filled on the next chat event from that user.
    """
    if timestamp is None:
        timestamp = datetime.now(timezone.utc).isoformat()

    with _connect() as conn:
        if t_id is not None:
            row = conn.execute(
                "SELECT id, t_login, t_past_logins FROM users WHERE t_id = ?",
                (t_id,),
            ).fetchone()

            if row is not None:
                # Known user — update in place, tracking any rename
                past_logins = json.loads(row["t_past_logins"])
                old_login = row["t_login"]
                if old_login != t_login:
                    print(
                        f"[user_db] Twitch login changed for t_id={t_id}: "
                        f"{old_login!r} → {t_login!r}"
                    )
                    if old_login not in past_logins:
                        past_logins.append(old_login)
                    # Remove any stale row that holds the new login without a t_id
                    conn.execute(
                        "DELETE FROM users WHERE t_login = ? AND t_id IS NULL",
                        (t_login,),
                    )
                conn.execute(
                    """
                    UPDATE users SET
                        t_login               = ?,
                        t_display_name        = COALESCE(?, t_display_name),
                        t_past_logins         = ?,
                        t_subscription_tier   = ?,
                        t_subscription_months = ?,
                        t_gift_sub_count      = ?,
                        t_messages_sent       = t_messages_sent + 1,
                        last_seen             = ?
                    WHERE t_id = ?
                    """,
                    (
                        t_login,
                        t_display_name,
                        json.dumps(past_logins),
                        t_subscription_tier,
                        t_subscription_months,
                        t_gift_sub_count,
                        timestamp,
                        t_id,
                    ),
                )
            else:
                # t_id not yet in DB — migrate existing row by login, or insert fresh
                old_row = conn.execute(
                    "SELECT id FROM users WHERE t_login = ?", (t_login,)
                ).fetchone()
                if old_row is not None:
                    # Back-fill t_id onto the pre-existing row
                    conn.execute(
                        """
                        UPDATE users SET
                            t_id                  = ?,
                            t_display_name        = COALESCE(?, t_display_name),
                            t_subscription_tier   = ?,
                            t_subscription_months = ?,
                            t_gift_sub_count      = ?,
                            t_messages_sent       = t_messages_sent + 1,
                            last_seen             = ?
                        WHERE t_login = ?
                        """,
                        (
                            t_id,
                            t_display_name,
                            t_subscription_tier,
                            t_subscription_months,
                            t_gift_sub_count,
                            timestamp,
                            t_login,
                        ),
                    )
                else:
                    conn.execute(
                        """
                        INSERT INTO users (
                            id, t_id, t_login, t_display_name,
                            t_subscription_tier, t_subscription_months,
                            t_gift_sub_count, t_messages_sent, first_seen, last_seen
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                        """,
                        (
                            str(uuid.uuid4()),
                            t_id,
                            t_login,
                            t_display_name,
                            t_subscription_tier,
                            t_subscription_months,
                            t_gift_sub_count,
                            timestamp,
                            timestamp,
                        ),
                    )
        else:
            # Legacy path: no t_id available, fall back to t_login keying
            conn.execute(
                """
                INSERT INTO users (
                    id, t_login, t_display_name, t_subscription_tier,
                    t_subscription_months, t_gift_sub_count, t_messages_sent,
                    first_seen, last_seen
                )
                VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)
                ON CONFLICT(t_login) DO UPDATE SET
                    t_display_name        = COALESCE(excluded.t_display_name, t_display_name),
                    t_subscription_tier   = excluded.t_subscription_tier,
                    t_subscription_months = excluded.t_subscription_months,
                    t_gift_sub_count      = excluded.t_gift_sub_count,
                    t_messages_sent       = t_messages_sent + 1,
                    last_seen             = excluded.last_seen
                """,
                (
                    str(uuid.uuid4()),
                    t_login,
                    t_display_name,
                    t_subscription_tier,
                    t_subscription_months,
                    t_gift_sub_count,
                    timestamp,
                    timestamp,
                ),
            )
        conn.commit()


def get_user(t_login: str) -> dict | None:
    """Return a user's full profile as a dict, or None if not found."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE t_login = ?", (t_login,)
        ).fetchone()
    if row is None:
        return None
    result = dict(row)
    result["notes"] = json.loads(result["notes"])
    result["t_past_logins"] = json.loads(result["t_past_logins"])
    result["d_past_usernames"] = json.loads(result["d_past_usernames"])
    return result


def upsert_discord_user(d_id: str, d_username: str | None = None) -> None:
    """
    Insert a new Discord-only user or update an existing one.
    If a row with this d_id already exists (including Twitch-linked rows),
    only updates d_username history and last_seen — Twitch data is untouched.
    """
    now = datetime.now(timezone.utc).isoformat()
    with _connect() as conn:
        row = conn.execute(
            "SELECT d_username, d_past_usernames FROM users WHERE d_id = ?", (d_id,)
        ).fetchone()
        if row:
            past = json.loads(row["d_past_usernames"])
            old = row["d_username"]
            if d_username and old and old != d_username and old not in past:
                past.append(old)
            conn.execute(
                """
                UPDATE users SET
                    d_username      = COALESCE(?, d_username),
                    d_past_usernames = ?,
                    last_seen       = ?
                WHERE d_id = ?
                """,
                (d_username, json.dumps(past), now, d_id),
            )
        else:
            conn.execute(
                """
                INSERT INTO users (id, d_id, d_username, notes, first_seen, last_seen)
                VALUES (?, ?, ?, '{}', ?, ?)
                """,
                (str(uuid.uuid4()), d_id, d_username, now, now),
            )
        conn.commit()


def get_user_by_discord(d_id: str) -> dict | None:
    """Return a user's full profile looked up by Discord ID, or None if not found."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE d_id = ?", (d_id,)
        ).fetchone()
    if row is None:
        return None
    result = dict(row)
    result["notes"] = json.loads(result["notes"])
    result["t_past_logins"] = json.loads(result["t_past_logins"])
    result["d_past_usernames"] = json.loads(result["d_past_usernames"])
    return result


def set_nickname(t_login: str, nickname: str) -> None:
    """Set or update a user's nickname (used organically by Berries)."""
    with _connect() as conn:
        conn.execute(
            "UPDATE users SET nickname = ? WHERE t_login = ?",
            (nickname, t_login),
        )
        conn.commit()


def set_nickname_for_discord(d_id: str, nickname: str) -> None:
    """Set or update a user's nickname looked up by Discord ID."""
    with _connect() as conn:
        conn.execute(
            "UPDATE users SET nickname = ? WHERE d_id = ?",
            (nickname, d_id),
        )
        conn.commit()


def add_note(t_login: str, key: str, value: str) -> None:
    """
    Merge a key/value pair into a user's JSON notes field.
    Creates the user row first if it doesn't exist.
    """
    now = datetime.now(timezone.utc).isoformat()
    with _connect() as conn:
        row = conn.execute(
            "SELECT notes FROM users WHERE t_login = ?", (t_login,)
        ).fetchone()
        if row is None:
            conn.execute(
                """
                INSERT INTO users (id, t_login, notes, first_seen, last_seen)
                VALUES (?, ?, ?, ?, ?)
                """,
                (str(uuid.uuid4()), t_login, json.dumps({key: value}), now, now),
            )
        else:
            notes = json.loads(row["notes"])
            notes[key] = value
            conn.execute(
                "UPDATE users SET notes = ? WHERE t_login = ?",
                (json.dumps(notes), t_login),
            )
        conn.commit()


def increment_streams_watched(t_logins: list[str]) -> None:
    """
    Increment t_streams_watched for all logins in the list.
    Call once per stream end for everyone who chatted that session.
    """
    if not t_logins:
        return
    with _connect() as conn:
        conn.executemany(
            "UPDATE users SET t_streams_watched = t_streams_watched + 1 WHERE t_login = ?",
            [(u,) for u in t_logins],
        )
        conn.commit()


def link_discord(t_login: str, d_id: str, d_username: str | None = None) -> dict:
    """
    Associate a Discord user ID with a Twitch login.

    Also updates d_username if provided, appending any changed username to
    d_past_usernames so name history is preserved.

    Creates a minimal user row if the Twitch login doesn't exist yet.
    If the Discord ID was previously linked to a different Twitch account,
    that old link is cleared first.

    Returns a dict with:
      status    — "already_linked" | "linked" | "updated"
      t_login   — the (normalised) Twitch login now stored
      previous  — the old Twitch login if the Discord ID moved, else None
    """
    t_login = t_login.lower().strip()
    now = datetime.now(timezone.utc).isoformat()

    with _connect() as conn:
        # Check if this Discord ID is already linked somewhere
        existing = conn.execute(
            "SELECT t_login FROM users WHERE d_id = ?", (d_id,)
        ).fetchone()

        if existing and existing["t_login"] == t_login:
            # Same link — still update d_username if it changed
            if d_username:
                _update_d_username(conn, t_login, d_username)
                conn.commit()
            return {"status": "already_linked", "t_login": t_login, "previous": None}

        # Clear the old d_id so one Discord account maps to one Twitch account
        if existing:
            conn.execute(
                "UPDATE users SET d_id = NULL WHERE d_id = ?", (d_id,)
            )

        # Upsert the target Twitch user row
        target = conn.execute(
            "SELECT id FROM users WHERE t_login = ?", (t_login,)
        ).fetchone()

        if target:
            _update_d_username(conn, t_login, d_username)
            conn.execute(
                "UPDATE users SET d_id = ? WHERE t_login = ?",
                (d_id, t_login),
            )
            status = "updated"
        else:
            conn.execute(
                """
                INSERT INTO users (id, t_login, d_id, d_username, notes, first_seen, last_seen)
                VALUES (?, ?, ?, ?, '{}', ?, ?)
                """,
                (str(uuid.uuid4()), t_login, d_id, d_username, now, now),
            )
            status = "linked"

        conn.commit()

    return {
        "status": status,
        "t_login": t_login,
        "previous": existing["t_login"] if existing else None,
    }


def _update_d_username(conn: sqlite3.Connection, t_login: str, d_username: str | None) -> None:
    """
    Update d_username for a row, appending the old value to d_past_usernames if it changed.
    No-op if d_username is None. Caller is responsible for committing.
    """
    if not d_username:
        return
    row = conn.execute(
        "SELECT d_username, d_past_usernames FROM users WHERE t_login = ?", (t_login,)
    ).fetchone()
    if row is None:
        return
    old = row["d_username"]
    past = json.loads(row["d_past_usernames"])
    if old and old != d_username and old not in past:
        past.append(old)
    conn.execute(
        "UPDATE users SET d_username = ?, d_past_usernames = ? WHERE t_login = ?",
        (d_username, json.dumps(past), t_login),
    )


def get_twitch_link(d_id: str) -> str | None:
    """Return the Twitch login linked to a Discord user ID, or None."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT t_login FROM users WHERE d_id = ?", (d_id,)
        ).fetchone()
    return row["t_login"] if row else None


def get_discord_for_twitch(t_login: str) -> str | None:
    """Return the Discord user ID linked to a Twitch login, or None."""
    t_login = t_login.lower().strip()
    with _connect() as conn:
        row = conn.execute(
            "SELECT d_id FROM users WHERE t_login = ?", (t_login,)
        ).fetchone()
    return row["d_id"] if row else None
