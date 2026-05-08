"""
scripts/dream.py

Berries' nightly dreaming phase — runs at 3am via systemd timer.

Phases:
  1. User memory consolidation
     - Reads today's daily interaction log (logs/daily_interactions/YYYY-MM-DD.json)
     - For each user with new activity, asks the LLM to update their `about` blurb
     - Writes updated blurbs back to users.db
     - Archives the processed log file to logs/daily_interactions/archive/

  2. Birthday check
     - Finds all users whose birthday (MM-DD) matches today
     - Generates a personalized birthday message from Berries for each
     - Posts to the Berries chat channel (so users can respond, not put on a pedestal)

  3. RAG summarization
     - Reads today's retrieval log (logs/daily_interactions/YYYY-MM-DD_retrievals.json)
     - For each (query → chunks) entry, distills factual content via LLM
     - Writes distilled summaries back to ChromaDB as source:summary entries
     - Archives the processed retrieval log

Designed as discrete phases so future work (stale summary regeneration, etc.) slots in cleanly.
"""

import asyncio
import hashlib
import json
import logging
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

# Allow running as a script from the repo root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from shared.config import (
    ANTHROPIC_ASSIST_MODEL,
    ANTHROPIC_CHAT_MODEL,
    DISCORD_BERRIES_CHAT_CHANNEL_ID,
    DISCORD_TOKEN,
    LOGS_DIR,
)
from shared.llm_client import get_completion
from shared.user_db import (
    get_all_users,
    get_birthday_users,
    set_about,
)

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("dream")

_INTERACTIONS_DIR = LOGS_DIR / "daily_interactions"
_ARCHIVE_DIR = _INTERACTIONS_DIR / "archive"


# ── Phase 1: User memory consolidation ───────────────────────────────────────

def _load_today_interactions(date_str: str) -> tuple[dict[str, list[str]], Path | None]:
    """
    Load the interaction log for the given date string (YYYY-MM-DD).
    Returns (interactions_dict, path). path is None if no file was found.
    """
    path = _INTERACTIONS_DIR / f"{date_str}.json"
    if not path.exists():
        return {}, None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data, path
    except Exception:
        log.exception("Failed to read interaction log at %s", path)
        return {}, None


def _build_user_index() -> dict[str, dict]:
    """
    Return two dicts for fast lookup:
      by_t_login[t_login] -> user row
      by_d_id[d_id]       -> user row
    """
    by_t_login: dict[str, dict] = {}
    by_d_id: dict[str, dict] = {}
    for user in get_all_users():
        if user.get("t_login"):
            by_t_login[user["t_login"]] = user
        if user.get("d_id"):
            by_d_id[user["d_id"]] = user
    return by_t_login, by_d_id


async def _update_about(user: dict, interactions: list[str]) -> str | None:
    """
    Ask the LLM to update the user's about blurb given their profile and today's interactions.
    Returns the new blurb, or None on failure.
    """
    name = user.get("nickname") or user.get("t_login") or user.get("d_username") or "this viewer"
    species = user.get("species")
    pronouns = user.get("pronouns")
    existing_about = user.get("about") or ""

    profile_lines = [f"Name: {name}"]
    if species:
        profile_lines.append(f"Species: {species}")
    if pronouns:
        profile_lines.append(f"Pronouns: {pronouns}")
    if existing_about:
        profile_lines.append(f"Current about blurb: {existing_about}")

    interactions_text = "\n".join(interactions[-40:])  # cap at 40 pairs

    prompt = (
        f"Here is what Berries currently knows about {name}:\n"
        f"{chr(10).join(profile_lines)}\n\n"
        f"Here are their interactions with Berries today:\n{interactions_text}\n\n"
        f"Please write an updated one paragraph about blurb for {name} that incorporates any new "
        f"information from today's interactions. Include their personality, interests, fursona details, "
        f"recurring topics, relationship to the stream — whatever is useful for Berries to remember. "
        f"Be specific and factual. Third person, present tense. No preamble, just the blurb, as it will be stored directly in the database. "
        f"If you don't have enough information to say anything new, just return the existing blurb unchanged."
    )
    system = "You maintain concise viewer profiles for a Twitch streamer's AI mascot. Be factual and specific."

    try:
        result = await get_completion(
            system_prompt=system,
            user_message=prompt,
            max_tokens=500,
            model=ANTHROPIC_ASSIST_MODEL,
        )
        return result.strip()
    except Exception:
        log.exception("Failed to generate about blurb for %s", name)
        return None


async def phase_user_memory(date_str: str) -> int:
    """Update `about` blurbs for all users with activity today. Returns count updated."""
    log.info("Phase 1: user memory consolidation for %s", date_str)

    interactions, log_path = _load_today_interactions(date_str)
    if not interactions:
        log.info("No interactions found for %s", date_str)
        return 0

    by_t_login, by_d_id = _build_user_index()
    updated = 0

    for user_key, pairs in interactions.items():
        if not pairs:
            continue
        user = by_t_login.get(user_key) or by_d_id.get(user_key)
        if not user:
            log.debug("No DB row found for key %r — skipping", user_key)
            continue

        name = user.get("nickname") or user.get("t_login") or user.get("d_username") or user_key
        log.info("Updating about for %r (%d interaction pair(s))", name, len(pairs))

        new_about = await _update_about(user, pairs)
        if not new_about:
            continue

        t_login = user.get("t_login")
        d_id = user.get("d_id")
        set_about(t_login=t_login, d_id=d_id if not t_login else None, about=new_about)
        log.info("  → %s", new_about[:120])
        updated += 1

    # Archive the processed log file
    if log_path:
        _ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
        dest = _ARCHIVE_DIR / log_path.name
        shutil.move(str(log_path), str(dest))
        log.info("Archived interaction log to %s", dest)

    return updated


# ── Phase 2: Birthday check ───────────────────────────────────────────────────

async def _post_to_berries_chat(message: str) -> bool:
    """Post a message to the Berries chat channel via Discord REST API."""
    if not DISCORD_TOKEN or not DISCORD_BERRIES_CHAT_CHANNEL_ID:
        log.warning("DISCORD_TOKEN or DISCORD_BERRIES_CHAT_CHANNEL_ID not set — skipping birthday post")
        return False
    try:
        import httpx
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"https://discord.com/api/v10/channels/{DISCORD_BERRIES_CHAT_CHANNEL_ID}/messages",
                headers={
                    "Authorization": f"Bot {DISCORD_TOKEN}",
                    "Content-Type": "application/json",
                },
                json={"content": message},
                timeout=10.0,
            )
        if resp.status_code in (200, 201):
            log.info("Posted birthday message to Berries chat channel")
            return True
        log.warning("Discord post failed: %s %s", resp.status_code, resp.text[:200])
        return False
    except Exception:
        log.exception("Failed to post birthday message to Discord")
        return False


async def _generate_birthday_message(user: dict) -> str:
    name = user.get("nickname") or user.get("t_login") or user.get("d_username") or "a dear friend"
    species = user.get("species")
    about = user.get("about")

    context_parts = []
    if species:
        context_parts.append(f"They are a {species}.")
    if about:
        context_parts.append(about)
    context = " ".join(context_parts)

    prompt = (
        f"Today is {name}'s birthday. Write a short, warm, in-character birthday message from Berries "
        f"(a spooky but affectionate forest demon). Reference their fursona or something personal if possible. "
        f"Keep it to 1-2 sentences. No markdown, no roleplay actions (like *does something*), no emojis. "
        + (f"Context about them: {context}" if context else "")
    )
    system = "You are Berries, a spooky and affectionate forest demon on a Twitch stream."

    try:
        result = await get_completion(
            system_prompt=system,
            user_message=prompt,
            max_tokens=100,
            model=ANTHROPIC_CHAT_MODEL,
        )
        return result.strip()
    except Exception:
        log.exception("Failed to generate birthday message for %s", name)
        return f"Happy birthday, {name}!"


async def phase_birthdays(today: datetime) -> int:
    """Check for birthdays today and post to Berries chat. Returns count greeted."""
    month_day = today.strftime("%m-%d")
    log.info("Phase 2: birthday check for %s", month_day)

    birthday_users = get_birthday_users(month_day)
    if not birthday_users:
        log.info("No birthdays today (%s)", month_day)
        return 0

    greeted = 0
    for user in birthday_users:
        name = user.get("nickname") or user.get("t_login") or user.get("d_username") or "someone"
        log.info("Generating birthday message for %r", name)
        message = await _generate_birthday_message(user)

        if user.get("d_id"):
            message = f"<@{user['d_id']}> {message}"

        posted = await _post_to_berries_chat(message)
        if posted:
            greeted += 1

    return greeted


# ── Phase 3: RAG summarization ────────────────────────────────────────────────

async def phase_rag_summarization(date_str: str) -> int:
    """
    Distill each (query → chunks) entry from today's retrieval log into a clean
    factual summary and write it to ChromaDB as source:summary.
    Returns the count of summaries written.
    """
    log.info("Phase 3: RAG summarization for %s", date_str)

    path = _INTERACTIONS_DIR / f"{date_str}_retrievals.json"
    if not path.exists():
        log.info("No retrieval log found for %s — skipping", date_str)
        return 0

    try:
        data: dict[str, list[str]] = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        log.exception("Failed to read retrieval log at %s", path)
        return 0

    from shared.chroma_client import upsert_summary

    system = (
        "You distill factual information from conversational logs for a Twitch streamer's AI mascot. "
        "Be concise and factual. Output only the distilled facts — no preamble, no commentary."
    )
    created = 0

    for query, chunks in data.items():
        if not chunks:
            continue

        passages = "\n---\n".join(chunks)
        prompt = (
            f"Given this search query: \"{query}\"\n\n"
            f"Extract only factual information from these passages that would help answer it — "
            f"facts about users, channel events, running jokes, past interactions. "
            f"Do not include response style, tone, or how Berries previously phrased things. "
            f"If there is nothing factual worth keeping, reply with only: SKIP\n\n"
            f"Passages:\n{passages}"
        )

        try:
            summary = await get_completion(
                system_prompt=system,
                user_message=prompt,
                max_tokens=256,
                model=ANTHROPIC_ASSIST_MODEL,
            )
        except Exception:
            log.exception("Distillation failed for query %r", query[:60])
            continue

        if not summary:
            continue
        summary = summary.strip()
        if summary.upper() == "SKIP" or not summary:
            log.debug("No facts extracted for query %r — skipping", query[:60])
            continue

        chunk_id = f"summary_{hashlib.sha256(query.encode()).hexdigest()[:16]}_{date_str}"
        try:
            upsert_summary(
                chunk_id=chunk_id,
                text=summary,
                metadata={
                    "source": "summary",
                    "generated_at": date_str,
                    "stale": False,
                    "origin_query": query[:200],
                },
            )
            log.info("Written summary %s for query %r", chunk_id, query[:60])
            created += 1
        except Exception:
            log.exception("Failed to write summary to ChromaDB for query %r", query[:60])

    # Archive the retrieval log
    _ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    dest = _ARCHIVE_DIR / path.name
    shutil.move(str(path), str(dest))
    log.info("Archived retrieval log to %s", dest)

    return created


# ── Entry point ───────────────────────────────────────────────────────────────

async def main() -> None:
    now = datetime.now(timezone.utc)
    # Dream at 3am means we're consolidating interactions from "yesterday" in UTC terms,
    # but local midnight is typically what matters — just process today's date.
    date_str = now.strftime("%Y-%m-%d")
    log.info("Dreaming started at %s (processing %s)", now.isoformat(), date_str)

    user_count = await phase_user_memory(date_str)
    log.info("Phase 1 complete: %d user about blurb(s) updated", user_count)

    birthday_count = await phase_birthdays(now)
    log.info("Phase 2 complete: %d birthday message(s) posted", birthday_count)

    rag_count = await phase_rag_summarization(date_str)
    log.info("Phase 3 complete: %d summary/summaries written to ChromaDB", rag_count)

    log.info("Dreaming complete.")


if __name__ == "__main__":
    asyncio.run(main())
