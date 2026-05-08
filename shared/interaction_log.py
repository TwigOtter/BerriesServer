"""
shared/interaction_log.py

Daily interaction log — one JSON file per calendar day.

File: logs/daily_interactions/YYYY-MM-DD.json
Schema:
  {
    "<t_login or d_id>": [
      "[nickname]: user message\\n[Berries]: berries response",
      ...
    ]
  }

Keys are the stable internal identifiers (t_login for Twitch users, Discord ID
for Discord-only users). The nickname appears inside the formatted strings so
the dreaming script can read them without a DB lookup.

Thread safety: we do a read-modify-write under a .lock file so concurrent
service instances (ingest + discord) don't clobber each other.
"""

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from shared.config import LOGS_DIR

_INTERACTIONS_DIR = LOGS_DIR / "daily_interactions"


def _today_path() -> Path:
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    _INTERACTIONS_DIR.mkdir(parents=True, exist_ok=True)
    return _INTERACTIONS_DIR / f"{date_str}.json"


def _load(path: Path) -> dict[str, list[str]]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save(path: Path, data: dict[str, list[str]]) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def log_interaction(
    *,
    user_key: str,
    nickname: str,
    user_message: str,
    berries_response: str,
) -> None:
    """
    Append one interaction pair to today's JSON log.

    user_key:       stable lookup key — t_login for Twitch, d_id for Discord-only
    nickname:       display name used inside the formatted string
    user_message:   the raw user message (without bot-prompt scaffolding)
    berries_response: Berries' response text
    """
    if not user_key or not berries_response:
        return

    entry = f"[{nickname}]: {user_message}\n[Berries]: {berries_response}"
    path = _today_path()
    lock = path.with_suffix(".lock")

    # Simple file-lock via O_CREAT | O_EXCL
    fd = None
    try:
        for _ in range(20):  # ~2s max wait
            try:
                fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                break
            except FileExistsError:
                import time
                time.sleep(0.1)
        if fd is None:
            # Give up on locking; best-effort append
            data = _load(path)
            data.setdefault(user_key, []).append(entry)
            _save(path, data)
            return
        os.close(fd)
        data = _load(path)
        data.setdefault(user_key, []).append(entry)
        _save(path, data)
    finally:
        try:
            lock.unlink(missing_ok=True)
        except Exception:
            pass
