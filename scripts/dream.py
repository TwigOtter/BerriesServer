"""
scripts/dream.py

Berries' nightly dreaming phase — runs at 3am local time (LOCAL_TIMEZONE)
via systemd timer (deploy/berries-dream.timer).

All dates are local-time calendar days, matching how the daily logs are keyed.
Each run processes EVERY unarchived day file older than today, not just
yesterday's — so missed runs (server down at 3am) catch up automatically.

Phases:
  1. User memory consolidation
     - Reads each unarchived daily interaction log (logs/daily_interactions/YYYY-MM-DD.json)
     - For each user with new activity, asks the LLM to update their `about` blurb
     - Writes updated blurbs back to users.db
     - Archives the processed log file to logs/daily_interactions/archive/

  2. Birthday check
     - Finds all users whose birthday (MM-DD) matches today (local)
     - Generates a personalized birthday message from Berries for each
     - Posts to the Berries chat channel (so users can respond, not put on a pedestal)

  3. RAG summarization (LLM step only)
     - Reads each unarchived retrieval log (logs/daily_interactions/YYYY-MM-DD_retrievals.json)
     - For each (query → chunks) entry, distills factual content via LLM
     - Persists summaries to logs/daily_interactions/pending/YYYY-MM-DD_pending_summaries.json
     - Archives the processed retrieval log
     - If a pending summaries file already exists for the date, skips the LLM
       step entirely — Phase 4 can retry the upsert without re-spending tokens

  4. Summary upsert
     - Reads every pending summaries file produced by Phase 3 (including ones
       left behind by earlier failed runs)
     - Upserts all summaries to ChromaDB as source:summary entries
     - On success, deletes the pending file
     - On failure, leaves the pending file in place so the next run can retry

Designed as discrete phases so future work (stale summary regeneration, etc.) slots in cleanly.
"""

import asyncio
import faulthandler
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

# Dump Python-level traceback to stderr on SIGSEGV — captured by journald.
faulthandler.enable()

# Prevent HuggingFace tokenizers from forking worker threads — causes SIGSEGV
# in short-lived processes when the tokenizer tries to parallelize on startup.
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("MKL_NUM_THREADS", "1")
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
# Disable ChromaDB's posthog telemetry background thread.
os.environ.setdefault("ANONYMIZED_TELEMETRY", "False")
# Prevent tqdm from starting its monitor thread (tqdm._monitor.TMonitor).
import tqdm as _tqdm
_tqdm.tqdm.monitor_interval = 0

# Allow running as a script from the repo root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from shared.config import (
    ANTHROPIC_ASSIST_MODEL,
    ANTHROPIC_CHAT_MODEL,
    DISCORD_BERRIES_CHAT_CHANNEL_ID,
    DISCORD_TOKEN,
    LOCAL_TZ,
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
_PENDING_DIR = _INTERACTIONS_DIR / "pending"

_INTERACTION_FILE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})\.json")
_RETRIEVAL_FILE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})_retrievals\.json")


def _unarchived_dates(today_str: str) -> tuple[list[str], list[str]]:
    """
    Scan logs/daily_interactions/ for unprocessed day files strictly older
    than `today_str` (today's file is still being written to).

    Returns (interaction_dates, retrieval_dates), each sorted ascending.
    Processing everything left behind — instead of exactly yesterday — means
    missed runs catch up automatically on the next night.
    """
    interaction_dates: list[str] = []
    retrieval_dates: list[str] = []
    if not _INTERACTIONS_DIR.exists():
        return interaction_dates, retrieval_dates
    for path in sorted(_INTERACTIONS_DIR.iterdir()):
        if not path.is_file():
            continue
        if m := _INTERACTION_FILE_RE.fullmatch(path.name):
            if m.group(1) < today_str:
                interaction_dates.append(m.group(1))
        elif m := _RETRIEVAL_FILE_RE.fullmatch(path.name):
            if m.group(1) < today_str:
                retrieval_dates.append(m.group(1))
    return interaction_dates, retrieval_dates


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
        f"Rewrite {name}'s about blurb to reflect what Berries should remember about them now. "
        f"Target 3–5 sentences, ~150 words max. Third person, present tense. Be specific and factual.\n\n"
        f"Treat this as a fresh distillation, not an append. Your job is to keep the blurb compact "
        f"and current: weave in anything important from today, but also prune stale or one-off details "
        f"that haven't come up again. Prioritize durable traits — personality, recurring interests, "
        f"fursona details, relationship to the stream — over passing chatter. The blurb should not grow "
        f"longer over time; if anything, it should get tighter as patterns emerge.\n\n"
        f"No preamble, just the blurb — it goes straight into the database. Do not repeat the "
        f"Name/Species/Pronouns header lines above; those are stored separately, and the blurb is "
        f"prose only. If today's interactions add nothing meaningful, you may tighten the existing "
        f"blurb or return it unchanged."
    )
    system = (
        "As part of a nightly process, you review Twitch and Discord user interactions with an AI chatbot named Berries. "
        "Your goal is to create and maintain concise, evolving user profiles that provide context for future interactions with the chatbot. "
        "Be factual and specific. Keep profiles current and relevant — rewrite, add, or prune details as the situation calls for."
    )

    try:
        result = await get_completion(
            system_prompt=system,
            user_message=prompt,
            max_tokens=300,
            model=ANTHROPIC_ASSIST_MODEL,
        )
        return _strip_profile_header(result.strip())
    except Exception:
        log.exception("Failed to generate about blurb for %s", name)
        return None


def _strip_profile_header(blurb: str) -> str:
    """
    Drop leading Name:/Species:/Pronouns: lines from a generated blurb.

    format_user_context() already emits those as structured fields, and once
    they leak into `about` they self-perpetuate — each nightly rewrite sees
    them in "Current about blurb" and keeps them. Stripping at write both
    prevents that and heals already-dirty rows on their next rewrite.
    """
    lines = blurb.splitlines()
    while lines and re.match(r"^(Name|Species|Pronouns)\s*:", lines[0].strip(), re.IGNORECASE):
        lines.pop(0)
    return "\n".join(lines).strip()


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

async def phase_rag_summarization(date_str: str) -> Path | None:
    """
    Distill each (query → chunks) entry from today's retrieval log into clean
    factual summaries via LLM, persist them to a pending JSON file, and archive
    the retrieval log. Phase 4 reads the pending file and writes to ChromaDB.

    If a pending summaries file already exists for `date_str`, skip the LLM
    step entirely and return the existing path — lets Phase 4 retry the upsert
    without re-spending API tokens.

    Returns the pending file path, or None if there's nothing to summarise.
    """
    log.info("Phase 3: RAG summarization for %s", date_str)

    pending_path = _PENDING_DIR / f"{date_str}_pending_summaries.json"
    retrieval_path = _INTERACTIONS_DIR / f"{date_str}_retrievals.json"

    # Pending file from a previous run — skip the LLM step.
    if pending_path.exists():
        log.info("Phase 3: pending summaries already exist at %s — skipping LLM step", pending_path)
        # If the retrieval log also still exists, archive it now (previous run
        # likely crashed between writing the pending file and archiving).
        if retrieval_path.exists():
            _ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
            shutil.move(str(retrieval_path), str(_ARCHIVE_DIR / retrieval_path.name))
            log.info("Archived stale retrieval log to %s", _ARCHIVE_DIR / retrieval_path.name)
        return pending_path

    if not retrieval_path.exists():
        log.info("No retrieval log found for %s — skipping", date_str)
        return None

    try:
        data: dict[str, list[str]] = json.loads(retrieval_path.read_text(encoding="utf-8"))
    except Exception:
        log.exception("Failed to read retrieval log at %s", retrieval_path)
        return None

    system = (
        "You distill factual information from conversational logs for a Twitch streamer's AI mascot. "
        "Be concise and factual. Output only the distilled facts — no preamble, no commentary."
    )

    pending: list[dict] = []

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
        # First-line check, not exact match: the model sometimes appends an
        # explanation after the verdict ("SKIP\n\nThe passages contain no...")
        # and an exact match would embed that whole reply as a summary.
        if not summary or summary.split("\n", 1)[0].strip().upper() == "SKIP":
            log.debug("No facts extracted for query %r — skipping", query[:60])
            continue

        chunk_id = f"summary_{hashlib.sha256(query.encode()).hexdigest()[:16]}_{date_str}"
        pending.append({
            "id": chunk_id,
            "document": summary,
            "metadata": {"source": "summary", "generated_at": date_str, "stale": False, "origin_query": query[:200]},
        })
        log.info("Distilled summary for query %r", query[:60])

    # Atomic write: write to a tmp file then rename, so a crash mid-write
    # doesn't leave a half-written JSON that Phase 4 fails to parse.
    _PENDING_DIR.mkdir(parents=True, exist_ok=True)
    tmp_path = pending_path.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(pending, indent=2), encoding="utf-8")
    tmp_path.rename(pending_path)
    log.info("Phase 3: wrote %d pending summaries to %s", len(pending), pending_path)

    # Archive the retrieval log — its data is now captured in the pending file.
    _ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    shutil.move(str(retrieval_path), str(_ARCHIVE_DIR / retrieval_path.name))
    log.info("Archived retrieval log to %s", _ARCHIVE_DIR / retrieval_path.name)

    return pending_path


# ── Phase 4: Summary upsert ───────────────────────────────────────────────────

def phase_upsert_summaries(pending_path: Path) -> int:
    """
    Read pending summaries from JSON and upsert them to ChromaDB IN A FRESH
    SUBPROCESS via scripts/upsert_pending.py.

    Why a subprocess: a direct in-process upsert segfaults reliably in
    chromadb 1.5.1's Rust backend after asyncio.run() has executed in this
    process — likely interpreter-state inherited from the closed event loop.
    A fresh interpreter has no such history and runs cleanly (same as how
    reindex_*.py scripts work).

    On success, delete the pending file. On failure, leave it in place so
    the next dream run can retry without re-spending LLM tokens.

    Returns the count of summaries upserted (0 on failure).
    """
    log.info("Phase 4: upsert summaries from %s (subprocess)", pending_path)

    try:
        count = len(json.loads(pending_path.read_text(encoding="utf-8")))
    except Exception:
        log.exception("Failed to read pending summaries at %s", pending_path)
        return 0

    if not count:
        log.info("Phase 4: no pending summaries — removing empty pending file")
        pending_path.unlink(missing_ok=True)
        return 0

    upsert_script = Path(__file__).parent / "upsert_pending.py"
    result = subprocess.run(
        [sys.executable, str(upsert_script), str(pending_path)],
        capture_output=True,
        text=True,
    )

    # Forward subprocess output into our log stream so journald sees it.
    for line in result.stdout.splitlines():
        log.info("[upsert_pending] %s", line)
    for line in result.stderr.splitlines():
        log.warning("[upsert_pending] %s", line)

    if result.returncode != 0:
        log.error(
            "Phase 4: upsert subprocess exited %d — leaving pending file in place for retry",
            result.returncode,
        )
        return 0

    pending_path.unlink(missing_ok=True)
    log.info("Phase 4 complete: %d summaries written; pending file removed", count)
    return count


# ── Entry point ───────────────────────────────────────────────────────────────

async def main() -> None:
    # The timer fires at 3am local time (deploy/berries-dream.timer), and the
    # daily logs are keyed by local date — so "everything older than today"
    # is exactly the finished days, including last evening's stream/Discord chat.
    now_local = datetime.now(LOCAL_TZ)
    today_str = now_local.strftime("%Y-%m-%d")
    interaction_dates, retrieval_dates = _unarchived_dates(today_str)
    log.info(
        "Dreaming started at %s — interaction day(s): %s; retrieval day(s): %s",
        now_local.isoformat(),
        ", ".join(interaction_dates) or "none",
        ", ".join(retrieval_dates) or "none",
    )

    for date_str in interaction_dates:
        user_count = await phase_user_memory(date_str)
        log.info("Phase 1 (%s) complete: %d user about blurb(s) updated", date_str, user_count)

    birthday_count = await phase_birthdays(now_local)
    log.info("Phase 2 complete: %d birthday message(s) posted", birthday_count)

    for date_str in retrieval_dates:
        pending_path = await phase_rag_summarization(date_str)
        if pending_path:
            log.info("Phase 3 (%s) complete: pending summaries staged at %s", date_str, pending_path)
        else:
            log.info("Phase 3 (%s) complete: nothing to summarise", date_str)


if __name__ == "__main__":
    # Phases 1, 2, and 3 (LLM calls) run inside asyncio.
    # Phase 4 spawns a subprocess for the ChromaDB upsert — see phase_upsert_summaries.
    asyncio.run(main())

    # Upsert every pending file, including any left behind by earlier failed runs.
    pending_files = sorted(_PENDING_DIR.glob("*_pending_summaries.json")) if _PENDING_DIR.exists() else []
    if not pending_files:
        log.info("Phase 4: no pending summaries files — skipping")
    for pending_path in pending_files:
        phase_upsert_summaries(pending_path)

    log.info("Dreaming complete.")
    os._exit(0)
