"""
shared/movie_db.py

SQLite-backed movie suggestion and watch history store.
Uses IMDB ID as the canonical identifier to prevent duplicates regardless
of how a title was typed.

Stored in the same DB file as users (USERS_DB_PATH).
"""

import sqlite3
from datetime import datetime, timezone

from shared.config import MOVIES_DB_PATH


def _connect() -> sqlite3.Connection:
    MOVIES_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(MOVIES_DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def init_movie_db() -> None:
    """Create movies table if it doesn't exist. Call once at service startup."""
    with _connect() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS movies (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                imdb_id      TEXT UNIQUE NOT NULL,
                title        TEXT NOT NULL,
                year         TEXT,
                suggested_by TEXT,
                suggested_at TEXT NOT NULL,
                status       TEXT NOT NULL DEFAULT 'suggested',
                watched_at   TEXT
            )
        """)
        conn.commit()


def get_suggestion(imdb_id: str) -> dict | None:
    """Return a movie row by IMDB ID, or None if not found."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM movies WHERE imdb_id = ?", (imdb_id,)
        ).fetchone()
    return dict(row) if row else None


def add_suggestion(imdb_id: str, title: str, year: str, suggested_by: str) -> None:
    """Insert a new movie suggestion."""
    now = datetime.now(timezone.utc).isoformat()
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO movies (imdb_id, title, year, suggested_by, suggested_at, status)
            VALUES (?, ?, ?, ?, ?, 'suggested')
            """,
            (imdb_id, title, year, suggested_by, now),
        )
        conn.commit()


def get_all_suggestions() -> list[dict]:
    """Return all movies with status='suggested', oldest first."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM movies WHERE status = 'suggested' ORDER BY suggested_at ASC"
        ).fetchall()
    return [dict(r) for r in rows]


def get_recent_watched(days: int = 365) -> list[dict]:
    """Return movies watched within the last N days."""
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT * FROM movies
            WHERE status = 'watched'
              AND watched_at >= datetime('now', ?)
            ORDER BY watched_at DESC
            """,
            (f"-{days} days",),
        ).fetchall()
    return [dict(r) for r in rows]


def get_all_watched() -> list[dict]:
    """Return all watched movies, most recent first."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM movies WHERE status = 'watched' ORDER BY watched_at DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def remove_suggestion(imdb_id: str) -> bool:
    """Delete a suggested movie by IMDB ID. Returns True if a row was deleted."""
    with _connect() as conn:
        cursor = conn.execute(
            "DELETE FROM movies WHERE imdb_id = ? AND status = 'suggested'", (imdb_id,)
        )
        conn.commit()
    return cursor.rowcount > 0


def mark_watched(imdb_id: str) -> None:
    """Set a movie's status to 'watched' and record the timestamp."""
    now = datetime.now(timezone.utc).isoformat()
    with _connect() as conn:
        conn.execute(
            "UPDATE movies SET status = 'watched', watched_at = ? WHERE imdb_id = ?",
            (now, imdb_id),
        )
        conn.commit()


def remove_watched(imdb_id: str) -> bool:
    """Delete a watched movie from history by IMDB ID. Returns True if a row was deleted."""
    with _connect() as conn:
        cursor = conn.execute(
            "DELETE FROM movies WHERE imdb_id = ? AND status = 'watched'", (imdb_id,)
        )
        conn.commit()
    return cursor.rowcount > 0
