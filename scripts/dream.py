"""
scripts/dream.py

Berries' nightly dreaming phase — runs at 3am local time (LOCAL_TIMEZONE)
via systemd timer (deploy/berries-dream.timer).

Evaluate it without touching anything:
    python scripts/dream.py --dry-run                       # tonight's real workload
    python scripts/dream.py --dry-run --date 2026-08-11     # replay a past day
    python scripts/dream.py --dry-run --date 2026-08-11 --limit 3   # cheap iteration
    python scripts/dream.py --dry-run --queries "what's my favorite mushroom"

--dry-run suppresses every side effect (users.db writes, the Discord birthday
post, the pending file, log archiving) and skips Phase 4 entirely, then prints
each generated memory beside the chunks a real run would have deleted.

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

  3. Memory consolidation (LLM step only)
     - Reads each unarchived retrieval log for that day's user messages; only
       the queries are used, not the stored chunk texts
     - Each query is re-run against ChromaDB (before asyncio starts) for its
       top chunks, WITH their IDs
     - Each query's chunks are consolidated into one prose memory — recounted
       the way a person remembers something, not a bulleted fact dump
     - Persists memory + the IDs it was built from to
       logs/daily_interactions/pending/YYYY-MM-DD_pending_summaries.json
     - Archives the processed retrieval log
     - If a pending summaries file already exists for the date, skips the LLM
       step entirely — Phase 4 can retry the upsert without re-spending tokens

  4. Summary upsert + source deletion
     - Reads every pending summaries file produced by Phase 3 (including ones
       left behind by earlier failed runs)
     - Upserts all summaries to ChromaDB as source:summary entries
     - Then deletes the source chunks folded into them, so the index holds the
       consolidated memory instead of the raw material
     - On success, deletes the pending file
     - On failure, leaves the pending file in place so the next run can retry

ChromaDB access is ordered read → asyncio → subprocess write, because its Rust
backend segfaults in-process once an event loop has run and closed.

NOTE: Phase 4 deletion means ChromaDB is no longer a pure derived index. A full
reindex from data/transcripts/*.jsonl rebuilds every consolidated-away chunk
and undoes the consolidation.

Designed as discrete phases so future work (stale summary regeneration, etc.) slots in cleanly.
"""

import argparse
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
import textwrap
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
    BASE_DIR,
    DISCORD_BERRIES_CHAT_CHANNEL_ID,
    DISCORD_TOKEN,
    LLM_BACKEND,
    LOCAL_TZ,
    LOGS_DIR,
    PERSONALITY_FILE,
    PROMPT_BUDGET_MARGIN,
    PROMPT_BUDGET_OVERHEAD_RATIO,
    PROMPT_BUDGET_PER_MESSAGE,
    VLLM_CONTEXT_TOKENS,
)
from shared.llm_client import get_completion
from shared.prompt_builder import format_user_context
from shared.tokenizer import count_tokens
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

# Set by --dry-run. When true, every side effect is suppressed: no users.db
# writes, no Discord post, no pending file, no log archiving, and Phase 4 does
# not run at all (so nothing is upserted and nothing is deleted).
DRY_RUN = False


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

        if DRY_RUN:
            print(f"\n[dry-run] would update about for {name}:\n  {new_about}\n")
            updated += 1
            continue

        t_login = user.get("t_login")
        d_id = user.get("d_id")
        set_about(t_login=t_login, d_id=d_id if not t_login else None, about=new_about)
        log.info("  → %s", new_about[:120])
        updated += 1

    # Archive the processed log file
    if log_path and not DRY_RUN:
        _ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
        dest = _ARCHIVE_DIR / log_path.name
        shutil.move(str(log_path), str(dest))
        log.info("Archived interaction log to %s", dest)

    return updated


# ── Phase 2: Birthday check ───────────────────────────────────────────────────

async def _post_to_berries_chat(message: str) -> bool:
    """Post a message to the Berries chat channel via Discord REST API."""
    if DRY_RUN:
        print(f"\n[dry-run] would post to Berries chat:\n  {message}\n")
        return True
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

# Prose memories run ~150-250 tokens; 500 leaves room for a multi-occasion
# recollection without handing the whole context window to the completion.
_CONSOLIDATION_MAX_TOKENS = 500
# Passes for the profile/budget fixed point in _fit_prompt(). Three is enough
# to settle every query measured on 2026-08-12; the fallback covers the rest.
_FIT_PROMPT_PASSES = 3


# "[DisplayName]: " prefix that ingest_api and the Discord watcher write.
_SPEAKER_RE = re.compile(r"^\[([^\]]+)\]:", re.MULTILINE)
# Discord display names carry a "#1234" discriminator in chunk text.
_DISCRIMINATOR_RE = re.compile(r"#\d+$")

_BERRIES_NAMES = {"berriesthedemon", "berries"}


def _normalise_name(name: str) -> str:
    """
    Fold a chat display name to a comparison key.

    Chunk prefixes and users.db rows disagree in predictable ways: chunks carry
    Discord display names with a discriminator ("Teeka#8081") while the row
    holds the username ("_teeka"). Stripping the discriminator and every
    non-alphanumeric character reconciles those without inventing a match.
    """
    name = _DISCRIMINATOR_RE.sub("", name.strip())
    return re.sub(r"[^a-z0-9]", "", name.lower())


def _build_speaker_index() -> dict[str, dict | None]:
    """
    Map normalised display name -> user row, for resolving chunk speakers.

    Names that collide after normalisation map to None and are then skipped:
    the whole point of this lookup is to stop guessing at people's pronouns, so
    an ambiguous match has to fail closed rather than pick a row.
    """
    index: dict[str, dict | None] = {}
    for user in get_all_users():
        for field in ("t_login", "t_display_name", "d_username", "nickname"):
            raw = user.get(field)
            if not raw:
                continue
            key = _normalise_name(str(raw))
            if not key or key in _BERRIES_NAMES:
                continue
            existing = index.get(key, "unset")
            if existing == "unset":
                index[key] = user
            elif existing is not None and existing.get("id") != user.get("id"):
                index[key] = None  # ambiguous — refuse to guess
    return index


def _speaker_profiles(hits: list[tuple[str, str, dict]], index: dict[str, dict | None]) -> str:
    """
    USER PROFILE blocks for the people speaking in these chunks, using the same
    formatter the live response pipeline uses (shared/prompt_builder.py).

    This is what carries pronouns into consolidation: most rows have none set,
    so the prompt's they/them default still does the heavy lifting, but where a
    user has told us, the memory gets it right.
    """
    seen: dict[str, str] = {}  # normalised key -> display name as written in chat
    for _cid, doc, _meta in hits:
        for raw in _SPEAKER_RE.findall(doc):
            key = _normalise_name(raw)
            if key and key not in _BERRIES_NAMES and key not in seen:
                seen[key] = _DISCRIMINATOR_RE.sub("", raw.strip())

    blocks: list[str] = []
    for key, display_name in seen.items():
        user = index.get(key)
        if not user:
            continue
        # Force they/them when the field is unset rather than omitting the
        # line: an absent Pronouns field leaves the model free to infer from a
        # username, which is exactly how Teeka (he/him) became "she". Only 19
        # of ~736 rows have pronouns set, so this is the common path.
        #
        # timezone is dropped so format_user_context omits its "Local time"
        # line — that renders the wall-clock time of the dream run, which is
        # meaningless inside a memory about something that happened weeks ago.
        user = {**user, "timezone": None, "pronouns": user.get("pronouns") or "they/them"}
        block = format_user_context(user, fallback_name=display_name)
        if block:
            blocks.append(block)
    return "\n\n".join(blocks)


def _consolidation_system_prompt(profiles: str) -> str:
    """
    System prompt for the consolidation pass: Berries' personality, the
    profiles of the people appearing in these chunks, and the recounting rules.

    The character material is here so the summarizer can recognise what is
    already established and leave it out — a memory that re-states "Berries is
    a forest demon who lives in a hollow" every night is pure noise.

    Curated lore is deliberately NOT injected: berries_bot/lore/facts.md is
    ~1950 tokens against a 4096 ceiling, and consolidation needs to know who
    the *people* are far more than it needs Berries' backstory.
    """
    personality = ""
    if PERSONALITY_FILE.exists():
        personality = PERSONALITY_FILE.read_text(encoding="utf-8").strip()

    return (
        "You are the part of Berries' mind that consolidates memories overnight, the way a "
        "sleeping brain replays and files the day. You are given chat excerpts that were "
        "retrieved together, and you write down what happened as a memory.\n\n"
        "Write in flowing prose, the way a person recounts something they remember. Past "
        "tense, third person, referring to Berries as 'Berries' and using people's names. "
        "Anchor each memory in time — start with the date it happened, like 'On 2026-07-26, "
        "Twig asked...'. If the excerpts cover separate occasions, give each its own short "
        "paragraph rather than blending them into one event.\n\n"
        "Record what was said and what it meant: what people asked, what Berries answered, "
        "what was revealed about someone, what was decided or joked about. Keep the details "
        "that a person would actually carry forward — preferences, opinions, running jokes, "
        "things that happened, how someone felt. It is fine to note Berries' own mood or how "
        "he answered when that is the memorable part.\n\n"
        "Use exactly the pronouns given in the profiles below. Never infer pronouns from a "
        "name, a fursona, or how someone writes — for anyone without a profile, use they/them. "
        "A memory is written once and re-read for months; a wrong guess here gets repeated "
        "back to that person.\n\n"
        "Do NOT write bullet points, headers, bold labels, or lists of extracted facts. Do "
        "not catalogue trivia nobody would remember, like who happened to be present or that "
        "someone is a community member. If nothing in the excerpts is worth remembering, "
        "reply with only: SKIP\n\n"
        "Below is who Berries is. Everything in it is already known — never restate it as if "
        "it were something newly learned. Use it only to judge what is genuinely new.\n\n"
        f"--- BERRIES' CHARACTER ---\n{personality}\n\n"
        f"--- THE PEOPLE IN THESE EXCERPTS ---\n"
        f"{profiles or '(no stored profiles — use they/them for everyone)'}"
    )


def _passage_budget(system: str, preamble: str) -> int:
    """
    Tokens left for chunk text after the system prompt, the instructions and
    the reserved completion.

    Only vLLM has a hard ceiling, and it cannot be raised (it is whatever the
    server was launched with) — overflowing it is a 400, not a truncation, so
    the whole night's consolidation would fail silently in the timer. Anthropic
    gets a generous cap that the 3-chunk default never approaches.

    The token count is corrected the same way shared/budget.py corrects the
    chat pipeline's, because it is wrong in the same direction: count_tokens()
    is tiktoken, the served model is Qwen with its own vocabulary, and the chat
    template's role framing is added server-side. Measured over 160 logged
    calls, vLLM charged 101-284 tokens more than the raw content count. This
    function used to reserve a flat 128-token margin against a mean undercount
    of 146 — so a prompt that fitted on paper could be rejected outright, which
    is what happened to one query on 2026-08-12.

    So budget on the *corrected* count: work out how many tokens the content
    may occupy after the ratio and the per-message framing are applied, then
    subtract what the system prompt and preamble already spend. Inverting
    estimate_prompt_tokens() rather than calling it, because the caller needs a
    raw-token allowance to measure candidate chunks against.

    Deliberately not gated on PROMPT_BUDGET_ENABLED: that flag governs whether
    the chat pipeline sheds context, and turning it off there must not let this
    script build a prompt the server will reject.
    """
    if LLM_BACKEND != "vllm":
        return 100_000
    # Two dispatched messages (system + user), matching estimate_prompt_tokens'
    # `len(dispatched) + 1` framing for a single user turn.
    framed = PROMPT_BUDGET_PER_MESSAGE * 2
    allowance = (
        VLLM_CONTEXT_TOKENS - _CONSOLIDATION_MAX_TOKENS - PROMPT_BUDGET_MARGIN - framed
    )
    content = int(allowance / PROMPT_BUDGET_OVERHEAD_RATIO)
    spent = count_tokens(system) + count_tokens(preamble)
    return max(content - spent, 0)


def _fit_passages(
    hits: list[tuple[str, str, dict]], budget: int
) -> list[tuple[str, str, dict]]:
    """
    Take chunks in rank order while they fit `budget` tokens.

    Returns the chunks that will actually appear in the prompt — the caller
    records exactly these as the summary's source_ids, because Phase 4 deletes
    them. A chunk dropped here must never be deleted: it was never consolidated
    into anything, so deleting it would be silent data loss.
    """
    kept: list[tuple[str, str, dict]] = []
    used = 0
    for cid, doc, meta in hits:
        cost = count_tokens(doc) + 16  # + the "[date]" header line
        if kept and used + cost > budget:
            break
        used += cost
        kept.append((cid, doc, meta))
    return kept


def _fit_prompt(
    hits: list[tuple[str, str, dict]],
    preamble: str,
    speaker_index: dict[str, dict | None],
) -> tuple[str, list[tuple[str, str, dict]]]:
    """
    Settle the system prompt and the chunk set together, and return both.

    The two depend on each other: the profile block is built from the speakers
    in the chunks, and the chunk budget is whatever the system prompt leaves
    over. Building profiles from *all* candidates charged the prompt for people
    who only speak in chunks the budget then dropped — on 2026-08-12 one query
    carried 1012 tokens of profiles, which cut its budget to 1147 and cost it a
    third of its source material.

    So iterate to a fixed point: profiles for the chunks currently kept, re-fit
    against the budget that leaves, repeat. Shedding a chunk can shed its
    speakers, which frees budget, which can take the chunk back — so the set
    can oscillate. The loop is capped and the fallback re-fits against the last
    system prompt, which means the pair handed back always fits: `kept` is
    always the output of `_fit_passages` under a budget derived from a system
    prompt whose profiles cover no fewer chunks than `kept` itself.

    The caller records exactly `kept` as the summary's source_ids, so the
    invariant from `_fit_passages` still holds — a chunk that never reached the
    prompt is never deleted.
    """
    kept = hits
    system = _consolidation_system_prompt(_speaker_profiles(kept, speaker_index))

    for _ in range(_FIT_PROMPT_PASSES):
        refit = _fit_passages(hits, _passage_budget(system, preamble))
        if refit == kept:
            return system, kept
        kept = refit
        system = _consolidation_system_prompt(_speaker_profiles(kept, speaker_index))

    # Out of passes — the set is oscillating. Narrow to what fits under the
    # current system prompt and rebuild from that; a subset can only shrink the
    # profile block, so the result still fits.
    kept = _fit_passages(kept, _passage_budget(system, preamble))
    return _consolidation_system_prompt(_speaker_profiles(kept, speaker_index)), kept


def _chunk_date(chunk_id: str, meta: dict) -> str:
    """Best available calendar date for a chunk, for anchoring the memory."""
    if date := meta.get("stream_date"):
        return str(date)
    if start := meta.get("start_time"):
        return str(start)[:10]
    m = re.search(r"(\d{4}-\d{2}-\d{2})", chunk_id)
    return m.group(1) if m else "an unknown date"


def fetch_chunks_for_queries(queries: list[str], n_results: int = 3) -> dict[str, dict]:
    """
    Re-query ChromaDB fresh for each of the day's user messages, returning
    {query: {"chunks": [(id, doc, meta)]}}.

    Deliberately not reusing the chunk texts stored in the retrieval log: those
    are post-windowing excerpts with no chunk IDs, and consolidation needs whole
    chunks it can then delete. Re-querying with the original user message also
    picks up whatever has been indexed since — including that same day's
    conversation, which is worth consolidating too.

    MUST be called before asyncio.run(): chromadb's Rust backend segfaults in
    this process once an event loop has run and closed (see
    phase_upsert_summaries), so all read-side Chroma work happens up front and
    all write-side work happens in a subprocess afterwards.
    """
    from shared.chroma_client import query_with_ids

    fetched: dict[str, dict] = {}
    for query in queries:
        try:
            hits = query_with_ids(query, n_results=n_results)
        except Exception:
            log.exception("Re-query failed for %r", query[:60])
            continue
        if hits:
            fetched[query] = {"chunks": hits}
    log.info(
        "Re-queried ChromaDB for %d quer(y/ies): %d with results, %d unique chunks",
        len(queries), len(fetched),
        len({cid for v in fetched.values() for cid, _, _ in v["chunks"]}),
    )
    return fetched


async def phase_rag_summarization(
    date_str: str,
    fetched: dict[str, dict],
) -> Path | None:
    """
    Consolidate each query's freshly-retrieved chunks into one prose memory,
    persist it to a pending JSON file alongside the IDs it was built from, and
    archive the retrieval log. Phase 4 upserts the memory and deletes those
    source chunks.

    If a pending summaries file already exists for `date_str`, skip the LLM
    step entirely and return the existing path — lets Phase 4 retry the upsert
    without re-spending API tokens.

    Returns the pending file path, or None if there's nothing to summarise.
    """
    log.info("Phase 3: memory consolidation for %s", date_str)

    pending_path = _PENDING_DIR / f"{date_str}_pending_summaries.json"
    retrieval_path = _INTERACTIONS_DIR / f"{date_str}_retrievals.json"

    # Pending file from a previous run — skip the LLM step. Ignored in a dry
    # run: the point there is to see freshly generated memories.
    if pending_path.exists() and not DRY_RUN:
        log.info("Phase 3: pending summaries already exist at %s — skipping LLM step", pending_path)
        # If the retrieval log also still exists, archive it now (previous run
        # likely crashed between writing the pending file and archiving).
        if retrieval_path.exists():
            _ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
            shutil.move(str(retrieval_path), str(_ARCHIVE_DIR / retrieval_path.name))
            log.info("Archived stale retrieval log to %s", _ARCHIVE_DIR / retrieval_path.name)
        return pending_path

    if not fetched:
        log.info("Nothing retrieved for %s — skipping", date_str)
        return None

    pending: list[dict] = []
    speaker_index = _build_speaker_index()

    for query, entry in fetched.items():
        hits = entry.get("chunks") or []
        if not hits:
            continue

        preamble = (
            f"These excerpts were all retrieved together when someone said: \"{query}\"\n\n"
            f"Write down what you remember from them, as prose. Each excerpt is labelled with "
            f"the date it happened — use those dates to anchor the memory.\n\n"
            f"Excerpts:\n"
        )
        # Only chunks that survive the budget go in the prompt, and only those
        # are recorded as sources — Phase 4 deletes exactly what was consolidated.
        system, hits = _fit_prompt(hits, preamble, speaker_index)
        if not hits:
            log.warning("No chunk fits the prompt budget for query %r — skipping", query[:60])
            continue

        passages = "\n---\n".join(
            f"[{_chunk_date(cid, meta)}]\n{doc}" for cid, doc, meta in hits
        )
        prompt = preamble + passages

        try:
            summary = await get_completion(
                system_prompt=system,
                user_message=prompt,
                max_tokens=_CONSOLIDATION_MAX_TOKENS,
                model=ANTHROPIC_ASSIST_MODEL,
                purpose="consolidate_memory",
            )
        except Exception:
            log.exception("Consolidation failed for query %r", query[:60])
            continue

        if not summary:
            continue
        summary = summary.strip()
        # First-line check, not exact match: the model sometimes appends an
        # explanation after the verdict ("SKIP\n\nThe passages contain no...")
        # and an exact match would embed that whole reply as a summary.
        if not summary or summary.split("\n", 1)[0].strip().upper() == "SKIP":
            log.debug("Nothing worth remembering for query %r — skipping", query[:60])
            continue

        chunk_id = f"summary_{hashlib.sha256(query.encode()).hexdigest()[:16]}_{date_str}"
        # Never list the new summary's own ID for deletion: re-running the
        # dream on a date it already processed regenerates the same ID, and
        # Phase 4 deletes after upserting — it would erase what it just wrote.
        source_ids = [cid for cid, _, _ in hits if cid != chunk_id]
        pending.append({
            "id": chunk_id,
            "document": summary,
            "metadata": {
                "source": "summary",
                "generated_at": date_str,
                "stale": False,
                "origin_query": query[:200],
                # How many of the consolidated chunks were themselves summaries
                # — a memory built only from summaries has been through the
                # rewrite mill before, and drift shows up here first.
                "from_summaries": sum(1 for cid, _, _ in hits if cid.startswith("summary_")),
            },
            "source_ids": source_ids,
        })
        log.info(
            "Consolidated %d chunk(s) for query %r", len(hits), query[:60],
        )

    if DRY_RUN:
        _report_dry_run(date_str, pending, fetched)
        return None

    # Atomic write: write to a tmp file then rename, so a crash mid-write
    # doesn't leave a half-written JSON that Phase 4 fails to parse.
    _PENDING_DIR.mkdir(parents=True, exist_ok=True)
    tmp_path = pending_path.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(pending, indent=2), encoding="utf-8")
    tmp_path.rename(pending_path)
    log.info("Phase 3: wrote %d pending summaries to %s", len(pending), pending_path)

    # Archive the retrieval log — its data is now captured in the pending file.
    if retrieval_path.exists():
        _ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
        shutil.move(str(retrieval_path), str(_ARCHIVE_DIR / retrieval_path.name))
        log.info("Archived retrieval log to %s", _ARCHIVE_DIR / retrieval_path.name)

    return pending_path


def _report_dry_run(date_str: str, pending: list[dict], fetched: dict[str, dict]) -> None:
    """Print each generated memory next to the chunks a real run would delete."""
    bar = "=" * 76
    print(f"\n{bar}\nDRY RUN — {date_str}: {len(pending)} memor{'y' if len(pending) == 1 else 'ies'} "
          f"from {len(fetched)} quer{'y' if len(fetched) == 1 else 'ies'}\n{bar}")

    # origin_query is truncated to 200 chars in metadata, so match on the same prefix.
    by_prefix = {q[:200]: v for q, v in fetched.items()}

    for item in pending:
        query = item["metadata"]["origin_query"]
        hits = {cid: (doc, meta) for cid, doc, meta in by_prefix.get(query, {}).get("chunks", [])}
        print(f"\n{'-' * 76}\nquery:  {query}\nid:     {item['id']}")
        gen = item["metadata"]["from_summaries"]
        if gen:
            print(f"        ({gen} of its {len(item['source_ids'])} sources "
                  f"{'was' if gen == 1 else 'were'} already a summary — a retelling of a retelling)")
        print("\nMEMORY:")
        print(textwrap.fill(item["document"], width=72, initial_indent="  ", subsequent_indent="  ")
              .replace("\n\n", "\n"))
        print(f"\nWOULD DELETE {len(item['source_ids'])} chunk(s):")
        for cid in item["source_ids"]:
            doc, _meta = hits.get(cid, ("", {}))
            preview = " ".join(doc.split())[:88]
            print(f"  - {cid}  ({count_tokens(doc)} tok)")
            print(f"      {preview}...")

    summarised = {i["metadata"]["origin_query"] for i in pending}
    skipped = [q for q in fetched if q[:200] not in summarised]
    if skipped:
        print(f"\n{'-' * 76}\nSKIPPED (model judged nothing worth remembering, or budget left no room):")
        for q in skipped:
            print(f"  - {q[:70]}")

    total_deleted = len({cid for i in pending for cid in i["source_ids"]})
    print(f"\n{bar}\nA real run would write {len(pending)} memor{'y' if len(pending) == 1 else 'ies'} "
          f"and delete {total_deleted} unique chunk(s). Nothing was changed.\n{bar}\n")


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

def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Berries' nightly dreaming phase.",
        epilog=(
            "Dry-run examples:\n"
            "  python scripts/dream.py --dry-run\n"
            "  python scripts/dream.py --dry-run --date 2026-08-12 --limit 5\n"
            "  python scripts/dream.py --dry-run --queries \"what's my favorite mushroom\"\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Generate memories and print them without changing anything: no users.db "
             "writes, no Discord post, no pending file, no archiving, and Phase 4 "
             "(upsert + delete) is skipped entirely.",
    )
    parser.add_argument(
        "--date", metavar="YYYY-MM-DD",
        help="Process only this date. Falls back to the archive/ copy of the retrieval "
             "log, so an already-processed day can be replayed.",
    )
    parser.add_argument(
        "--limit", type=int, metavar="N",
        help="Only consolidate the first N queries of each day — keeps prompt-tuning "
             "iterations cheap.",
    )
    parser.add_argument(
        "--queries", nargs="+", metavar="Q",
        help="Consolidate these queries instead of reading a retrieval log. Implies "
             "--dry-run.",
    )
    args = parser.parse_args()
    if args.queries:
        args.dry_run = True
    return args


def _queries_for_date(date_str: str) -> list[str]:
    """
    The day's user messages that triggered a lookup — keys of the retrieval log.

    Falls back to the archived copy so a dry run can replay a day that a real
    run already processed and archived.
    """
    path = _INTERACTIONS_DIR / f"{date_str}_retrievals.json"
    if not path.exists():
        path = _ARCHIVE_DIR / f"{date_str}_retrievals.json"
    if not path.exists():
        return []
    try:
        data: dict[str, list[str]] = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        log.exception("Failed to read retrieval log at %s", path)
        return []
    # Only the keys matter. The stored chunk texts are post-windowing excerpts
    # with no IDs; Phase 3 re-queries instead of reusing them.
    return [q for q in data if q]


async def main(prefetched: dict[str, dict], args: argparse.Namespace) -> None:
    # The timer fires at 3am local time (deploy/berries-dream.timer), and the
    # daily logs are keyed by local date — so "everything older than today"
    # is exactly the finished days, including last evening's stream/Discord chat.
    now_local = datetime.now(LOCAL_TZ)
    today_str = now_local.strftime("%Y-%m-%d")
    interaction_dates, retrieval_dates = _unarchived_dates(today_str)
    if args.date:
        interaction_dates = [d for d in interaction_dates if d == args.date]
        retrieval_dates = [args.date]
    if args.queries:
        # Ad-hoc queries aren't tied to a day's log; only Phase 3 runs.
        interaction_dates, retrieval_dates = [], list(prefetched)
    log.info(
        "Dreaming started at %s — interaction day(s): %s; retrieval day(s): %s",
        now_local.isoformat(),
        ", ".join(interaction_dates) or "none",
        ", ".join(retrieval_dates) or "none",
    )

    for date_str in interaction_dates:
        user_count = await phase_user_memory(date_str)
        log.info("Phase 1 (%s) complete: %d user about blurb(s) updated", date_str, user_count)

    if not args.queries:
        birthday_count = await phase_birthdays(now_local)
        log.info("Phase 2 complete: %d birthday message(s) posted", birthday_count)

    for date_str in retrieval_dates:
        pending_path = await phase_rag_summarization(date_str, prefetched.get(date_str, {}))
        if pending_path:
            log.info("Phase 3 (%s) complete: pending summaries staged at %s", date_str, pending_path)
        elif DRY_RUN:
            log.info("Phase 3 (%s) complete: see dry-run report above", date_str)
        else:
            log.info("Phase 3 (%s) complete: nothing to summarise", date_str)


if __name__ == "__main__":
    _args = _parse_args()
    DRY_RUN = _args.dry_run
    if DRY_RUN:
        log.info("DRY RUN — no writes, no deletions, Phase 4 skipped")

    # ChromaDB reads happen HERE, before any event loop has run: chromadb's
    # Rust backend segfaults in-process once asyncio.run() has completed (see
    # phase_upsert_summaries), so the ordering is read → asyncio → subprocess
    # write. A pending file from a previous run makes its date's fetch
    # unnecessary — Phase 3 will skip the LLM step for it anyway.
    _today = datetime.now(LOCAL_TZ).strftime("%Y-%m-%d")
    _prefetched: dict[str, dict] = {}

    if _args.queries:
        _prefetched[_today] = fetch_chunks_for_queries(_args.queries)
    else:
        _, _retrieval_dates = _unarchived_dates(_today)
        if _args.date:
            _retrieval_dates = [_args.date]
        for _date in _retrieval_dates:
            if (_PENDING_DIR / f"{_date}_pending_summaries.json").exists() and not DRY_RUN:
                log.info("Skipping re-query for %s — pending summaries already staged", _date)
                continue
            _queries = _queries_for_date(_date)
            if _args.limit:
                _queries = _queries[: _args.limit]
            if _queries:
                log.info("Re-querying ChromaDB for %d quer(y/ies) from %s", len(_queries), _date)
                _prefetched[_date] = fetch_chunks_for_queries(_queries)
            else:
                log.info("No retrieval log entries found for %s", _date)

    # Phases 1, 2, and 3 (LLM calls) run inside asyncio.
    # Phase 4 spawns a subprocess for the ChromaDB upsert — see phase_upsert_summaries.
    asyncio.run(main(_prefetched, _args))

    if DRY_RUN:
        log.info("Dry run complete — nothing was written or deleted.")
        # os._exit skips interpreter cleanup, including flushing stdout — the
        # dry-run report is block-buffered when piped and would be discarded.
        sys.stdout.flush()
        os._exit(0)

    # Upsert every pending file, including any left behind by earlier failed runs.
    pending_files = sorted(_PENDING_DIR.glob("*_pending_summaries.json")) if _PENDING_DIR.exists() else []
    if not pending_files:
        log.info("Phase 4: no pending summaries files — skipping")
    for pending_path in pending_files:
        phase_upsert_summaries(pending_path)

    log.info("Dreaming complete.")
    sys.stdout.flush()
    os._exit(0)
