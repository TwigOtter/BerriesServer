"""
shared/ask_berries.py

Central hub for all Berries LLM interactions.

All pathways that result in a Berries response go through one of the ask_berries_*
functions here. Lower-level plumbing (get_completion, rewrite_queries, ChromaDB) is
consumed from shared/ but never called directly by service code.

Public API:
    ask_berries()                   — raw LLM call, no logging
    ask_berries_discord()           — one-off Discord response (currently unused, but available for simple replies that don't need the full @mention pipeline)
    ask_berries_twitch()            — full Twitch @mention pipeline (ChromaDB + nickname + log)
    ask_berries_discord_mention()   — full Discord @mention pipeline (ChromaDB + nickname + log)
    ask_berries_movie_announcement()    — movie night announcement + gif query (Twig-directed)
    ask_berries_twitch_going_live()     — going-live announcement + gif query (Twig-directed)
"""

import logging
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from shared.config import PERSONALITY_FILE
from shared.llm_client import get_completion, rewrite_queries
from shared.chroma_client import query_chroma_multi
from shared.prompt_builder import (
    ContextType,
    build_system_prompt,
    format_chroma_context,
    format_recent_chunks,
)
from shared.call_logger import log_llm_call

log = logging.getLogger(__name__)

# System instruction used when Berries is collaborating with Twig to write content
# for his Discord community rather than responding to a viewer.
_TWIG_COLLABORATION_INSTRUCTION = """\
YOUR CURRENT TASK:
You are currently assisting Twig in communicating with his Discord community. \
Your response will be posted verbatim to the server — write for the audience, not back to Twig."""


# ── Internal helpers ─────────────────────────────────────────────────────────

def _load_personality() -> str:
    if PERSONALITY_FILE.exists():
        return PERSONALITY_FILE.read_text(encoding="utf-8").strip()
    log.warning("personality.txt not found, using fallback prompt.")
    return "You are Berries, a spooky and playful forest demon on a Twitch stream. Keep responses short and in character."


def _get_nickname_twitch(t_login: str) -> str:
    """Return the user's nickname if set, otherwise their t_login."""
    from shared.user_db import get_user
    user = get_user(t_login)
    return (user.get("nickname") or t_login) if user else t_login


def _get_nickname_discord(discord_id: str, display_name: str) -> str:
    """Return the user's nickname from user_db, falling back to their Discord display name."""
    from shared.user_db import get_twitch_link, get_user, get_user_by_discord
    t_login = get_twitch_link(discord_id)
    db_user = get_user(t_login) if t_login else get_user_by_discord(discord_id)
    return (db_user.get("nickname") or display_name) if db_user else display_name


async def _chroma_context(
    query: str,
    recent_context: str,
    username: str,
) -> tuple[list[str], list[str] | None]:
    """
    Run query rewriting + ChromaDB retrieval.
    Returns (docs, queries_used). docs is empty on SKIP or failure.
    queries_used is None when the rewriter returned SKIP.
    """
    try:
        search_queries = await rewrite_queries(query, recent_context, username)
        if search_queries is None:  # rewriter returned SKIP
            log.debug("rewrite_queries returned SKIP for query: %.80r", query)
            return [], None
        docs = query_chroma_multi(search_queries)
        log.debug("ChromaDB returned %d doc(s) for %d rewritten query/queries", len(docs), len(search_queries))
        return docs, search_queries
    except Exception:
        log.exception("ChromaDB query/rewrite failed (no context injected)")
        return [], []


def cleanup_response(text: str) -> str:
    """Remove italicised roleplay lines and collapse double line breaks."""
    text = re.sub(r"^\*[^*\n]+\*\s*$", "", text, flags=re.MULTILINE)
    text = re.sub(r"\n{2,}", "\n", text)
    return text.strip()


# ── Public API ───────────────────────────────────────────────────────────────

async def ask_berries(
    system_prompt: str,
    user_message: str,
    max_tokens: int = 256,
) -> str | None:
    """Raw LLM call via get_completion(). No logging — callers handle that."""
    return await get_completion(system_prompt=system_prompt, user_message=user_message, max_tokens=max_tokens)


async def ask_berries_discord(
    user_message: str,
    context_type: ContextType = ContextType.DISCORD_MENTION,
    context: str = "",
    max_tokens: int = 600,
) -> str | None:
    """
    Builds the system prompt from personality + context_type and returns a cleaned response.
    Use for short in-character replies that aren't full @mention or announcement pipelines
    (e.g. movie suggestion rejection).
    """
    system = build_system_prompt(_load_personality(), context_type, context)
    log.debug("ask_berries_discord — user_message: %.120r", user_message)
    response = await ask_berries(system, user_message, max_tokens=max_tokens)
    log.debug("ask_berries_discord — response: %.120r", response)
    return cleanup_response(response) if response is not None else None


async def ask_berries_twitch(
    query: str,
    username: str,
    tts: bool,
    recent_chunks: list[dict],
    recent_buffer_text: str = "",
) -> str | None:
    """
    Full Twitch @mention pipeline.

    Looks up nickname from user_db internally, queries ChromaDB with rewritten queries,
    assembles the system prompt with short- and long-term memory, calls the LLM, and logs.

    Args:
        query:              Raw viewer message (used for ChromaDB retrieval and logging).
        username:           Twitch t_login; used for nickname lookup.
        tts:                Whether TTS mode is active (affects response instructions).
        recent_chunks:      Deque of recently flushed chunks for short-term memory context.
        recent_buffer_text: Last N in-progress buffer entries for query rewriting context.
    """
    nickname = _get_nickname_twitch(username) if username else ""
    if username and nickname:
        user_message = (
            f"A viewer named {nickname} (username: {username}, call them '{nickname}') "
            f'says: "{query}" -- Please respond directly to them.'
        )
    else:
        user_message = query

    context_type = ContextType.TWITCH_TTS if tts else ContextType.TWITCH_CHAT
    context_parts: list[str] = []

    # Long-term memory: semantically relevant past chunks via ChromaDB
    docs, search_queries = await _chroma_context(
        query,
        recent_context=recent_buffer_text,
        username=username or "a viewer",
    )
    if docs:
        context_parts.append(format_chroma_context(docs))

    # Short-term memory: last N flushed chunks from this session
    if recent_chunks:
        context_parts.append(format_recent_chunks([c["text"] for c in recent_chunks]))

    system_prompt = build_system_prompt(
        _load_personality(),
        context_type,
        "\n\n".join(context_parts),
    )
    response = await ask_berries(system_prompt=system_prompt, user_message=user_message)

    log_llm_call(
        service="twitch",
        username=username or "",
        raw_message=query,
        rewrite_queries=search_queries,
        system_prompt=system_prompt,
        user_message=user_message,
        response=response,
    )
    return response


async def ask_berries_discord_mention(
    query: str,
    display_name: str,
    discord_id: str,
    channel_history: str,
    created_at: datetime,
) -> str | None:
    """
    Full Discord @mention pipeline.

    Looks up nickname from user_db internally (via discord_id), queries ChromaDB,
    assembles the system prompt, calls the LLM, cleans the response, and logs.

    Args:
        query:           Raw message content (with @mention token already replaced).
        display_name:    Discord display name shown in the user_message to Berries.
        discord_id:      Discord user ID string; used for nickname lookup in user_db.
        channel_history: Pre-fetched formatted channel history string (from _get_channel_history).
        created_at:      Message timestamp; formatted into the user_message for Berries.
    """
    nickname = _get_nickname_discord(discord_id, display_name)
    nickname_str = f" (nickname: {nickname})" if nickname != display_name else ""
    date_str = (
        created_at.replace(tzinfo=timezone.utc)
        .astimezone(ZoneInfo("America/Chicago"))
        .strftime("%A, %Y-%m-%d %H:%M:%S")
    )
    user_message = f"(datetime: {date_str} [US Central Time]) {display_name}{nickname_str} said: {query}"

    # Long-term memory via ChromaDB (use channel_history as recent_context for query rewriting)
    docs, search_queries = await _chroma_context(
        query,
        recent_context=channel_history,
        username=display_name,
    )
    chroma_block = format_chroma_context(docs) if docs else ""

    system_suffix = "\n\n".join(filter(None, [chroma_block, channel_history]))
    system_prompt = build_system_prompt(_load_personality(), ContextType.DISCORD_MENTION, system_suffix)

    log.debug("ask_berries_discord_mention — user_message: %.120r", user_message)
    response = await ask_berries(system_prompt=system_prompt, user_message=user_message, max_tokens=600)
    response = cleanup_response(response) if response else response
    log.debug("ask_berries_discord_mention — response: %.120r", response)

    log_llm_call(
        service="discord",
        username=display_name,
        raw_message=query,
        rewrite_queries=search_queries,
        system_prompt=system_prompt,
        user_message=user_message,
        response=response,
    )
    return response


async def ask_berries_movie_announcement(
    movie_title: str,
    movie_year: str,
    notes: str = "",
) -> tuple[str, str] | None:
    """
    Movie night announcement pipeline. Returns (announcement, gif_query) or None on failure.

    Queries ChromaDB for past discussions about the movie, then makes two sequential LLM calls:
      1. Twig asks Berries to write a Discord announcement → announcement text
      2. Berries picks a Giphy search query to accompany the announcement → gif_query string

    The system prompt uses the collaboration framing (Twig-directed) rather than the
    @mention response instructions, since Berries is writing *for* the audience, not *to* a viewer.
    """
    docs, _ = await _chroma_context(
        query=f"{movie_title} ({movie_year}) movie",
        recent_context="",
        username="Twig",
    )
    chroma_block = format_chroma_context(docs) if docs else ""
    personality = _load_personality()
    system = "\n\n".join(filter(None, [personality, chroma_block, _TWIG_COLLABORATION_INSTRUCTION]))

    user_msg = (
        f"[Twig]: Hey Berries, tonight we're going to be watching **{movie_title} ({movie_year})** "
        f"for our weekly movie night starting right now. Can you write a friendly announcement to the "
        f"Discord server that tells people what we will be watching, your silly or snarky personal "
        f"opinions on the movie, and then tell people that they're welcome to join if that sounds like "
        f"a good time to them? Don't pressure people, just let them know what's happening and tell them "
        f"they're welcome to join. Please write your message for the audience, as your response will be "
        f"posted verbatim to the announcements channel for all to see."
    )
    if notes:
        user_msg += f" Additional context from Twig: {notes}"

    log.debug("ask_berries_movie_announcement — requesting announcement for %r (%s)", movie_title, movie_year)
    announcement = await ask_berries(system_prompt=system, user_message=user_msg, max_tokens=600)
    if not announcement:
        log.warning("ask_berries_movie_announcement — LLM returned empty announcement")
        return None
    announcement = cleanup_response(announcement)

    gif_query = await ask_berries(
        system_prompt=system,
        user_message=(
            "Thank you! Can you now generate a search query for Giphy to find a gif that fits the vibe "
            "of your announcement? Reply with ONLY the search query, 2-5 words, no punctuation, no explanation."
        ),
        max_tokens=32,
    )
    gif_query = (gif_query or "").strip()
    log.debug("ask_berries_movie_announcement — gif_query: %r", gif_query)

    return announcement, gif_query


async def ask_berries_twitch_going_live(
    stream_title: str,
    stream_category: str,
) -> tuple[str, str] | None:
    """
    Going-live announcement pipeline. Returns (announcement, gif_query) or None on failure.

    Makes two sequential LLM calls under the collaboration framing:
      1. Twig asks Berries to write a going-live Discord announcement → announcement text
      2. Berries picks a Giphy search query to accompany the announcement → gif_query string
    """
    personality = _load_personality()
    system = "\n\n".join([personality, _TWIG_COLLABORATION_INSTRUCTION])

    user_msg = (
        f"[Twig]: Hey Berries, I just went live! Stream title: '{stream_title}', "
        f"category: '{stream_category}'. Can you write a short (2-3 sentence) friendly going-live announcement for the "
        f"Discord server that tells people what's to expect from the stream, your silly or snarky "
        f"personal opinions on the stream title/category, and then tell people that they're welcome to "
        f"join if that sounds like a good time to them? Don't pressure people, just let them know what's "
        f"happening and tell them they're welcome to join. "
        f"Please write your message for the audience, as your response will be posted verbatim."
    )

    log.debug("ask_berries_twitch_going_live — requesting announcement for %r / %r", stream_title, stream_category)
    announcement = await ask_berries(system_prompt=system, user_message=user_msg, max_tokens=400)
    if not announcement:
        log.warning("ask_berries_twitch_going_live — LLM returned empty announcement")
        return None
    announcement = cleanup_response(announcement)

    gif_query = await ask_berries(
        system_prompt=system,
        user_message=(
            "Great! Now generate a Giphy search query for a gif that fits the vibe of your announcement. "
            "Reply with ONLY the search query, 2-5 words, no punctuation, no explanation."
        ),
        max_tokens=32,
    )
    gif_query = (gif_query or "").strip()
    log.debug("ask_berries_twitch_going_live — gif_query: %r", gif_query)

    return announcement, gif_query
