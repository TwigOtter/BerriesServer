"""
shared/ask_berries.py

Central hub for all Berries LLM interactions.

All pathways that result in a Berries response go through one of the ask_berries_*
functions here. Lower-level plumbing (get_completion, retrieve_context, ChromaDB)
is consumed from shared/ but never called directly by service code.

Public API:
    ask_berries()                   — raw LLM call, no logging
    ask_berries_discord()           — one-off Discord response (currently unused, but available for simple replies that don't need the full @mention pipeline)
    ask_berries_twitch()            — full Twitch @mention pipeline (ChromaDB + nickname + log)
    ask_berries_discord_mention()   — full Discord @mention pipeline (ChromaDB + nickname + log)
    ask_berries_twitch_going_live() — going-live announcement + gif query (Twig-directed)
"""

import asyncio
import logging
import re

from shared import trace
from shared.config import AGENT_TOOLS_ENABLED, PERSONALITY_FILE
from shared.context_providers import (
    BerriesRequest,
    ChannelHistoryProvider,
    ChromaContextProvider,
    RecentChunksProvider,
    UserProfileProvider,
    build_context,
)
from shared.llm_client import get_completion
from shared.prompt_builder import ContextType, build_system_prompt
from shared.interaction_log import log_interaction

log = logging.getLogger(__name__)

# Context blocks per platform, in prompt order. Adding a new context source
# (server rules, tool results, ...) means adding a provider here, not a new
# pipeline.
#
# LoreProvider is intentionally absent: facts.md is reachable through ordinary
# vector search instead (the source:"lore" filter in query_chroma_multi is
# lifted to match). This is a known-worse interim — measured 3/6 on the
# fabrication check vs 5/6 when injected — accepted while personality.txt and
# facts.md are deduplicated and lore moves to its own dedicated query.
_TWITCH_PROVIDERS = [
    ChromaContextProvider(),
    UserProfileProvider(),
    RecentChunksProvider(),
]
_DISCORD_MENTION_PROVIDERS = [
    ChromaContextProvider(),
    UserProfileProvider(),
    ChannelHistoryProvider(),
]


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


def cleanup_response(text: str) -> str:
    """Remove italicised roleplay lines and collapse double line breaks."""
    text = re.sub(r"^\*[^*\n]+\*\s*$", "", text, flags=re.MULTILINE)
    text = re.sub(r"\n{2,}", "\n", text)
    return text.strip()


# ── Public API ───────────────────────────────────────────────────────────────

async def ask_berries(
    system_prompt: str,
    user_message: str = "",
    max_tokens: int = 256,
    messages: list[dict] | None = None,
    purpose: str = "chat_response",
) -> str | None:
    """Raw LLM call via get_completion(). No logging — callers handle that.

    messages: full conversation history. When provided, takes precedence over user_message.
    purpose:  label for the call in logs/traces (see shared/llm_client.py).
    """
    return await get_completion(
        system_prompt=system_prompt, user_message=user_message,
        max_tokens=max_tokens, messages=messages, purpose=purpose,
    )


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
    with trace.trace("discord_oneoff", context_type=context_type.name):
        system = build_system_prompt(_load_personality(), context_type, context)
        trace.add(system_prompt=system, user_message=user_message)
        log.debug("ask_berries_discord — user_message: %.120r", user_message)
        with trace.step("llm_response"):
            response = await ask_berries(system, user_message, max_tokens=max_tokens)
        log.debug("ask_berries_discord — response: %.120r", response)
        response = cleanup_response(response) if response is not None else None
        trace.add(response=response)
        return response


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
    with trace.trace("twitch_mention", username=username, query=query, tts=tts):
        with trace.step("nickname_lookup"):
            nickname = await asyncio.to_thread(_get_nickname_twitch, username) if username else ""
        if username and nickname:
            user_message = (
                f"A viewer named {nickname} (username: {username}, call them '{nickname}') "
                f'says: "{query}" -- Please respond directly to them.'
            )
        else:
            user_message = query

        context_type = ContextType.TWITCH_TTS if tts else ContextType.TWITCH_CHAT

        req = BerriesRequest(
            query=query,
            display_name=nickname or username or "a viewer",
            t_login=username or None,
            recent_context=recent_buffer_text,
            recent_chunks=[c["text"] for c in recent_chunks],
        )
        context = await build_context(_TWITCH_PROVIDERS, req)

        system_prompt = build_system_prompt(_load_personality(), context_type, context)
        trace.add(system_prompt=system_prompt, user_message=user_message)
        with trace.step("llm_response"):
            response = await ask_berries(system_prompt=system_prompt, user_message=user_message, max_tokens=80)
        trace.add(response=response)

        if username and response:
            with trace.step("log_interaction"):
                log_interaction(
                    user_key=username,
                    nickname=nickname or username,
                    user_message=query,
                    berries_response=response,
                )
        return response


async def ask_berries_discord_mention(
    query: str,
    display_name: str,
    discord_id: str,
    channel_history: str,
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
    """
    with trace.trace("discord_mention", username=display_name, discord_id=discord_id, query=query):
        with trace.step("nickname_lookup"):
            nickname = await asyncio.to_thread(_get_nickname_discord, discord_id, display_name)
        user_message = f"{nickname} said: {query}"

        req = BerriesRequest(
            query=query,
            display_name=display_name,
            discord_id=discord_id,
            # channel_history doubles as recency context for query rewriting
            recent_context=channel_history,
            channel_history=channel_history,
        )
        context = await build_context(_DISCORD_MENTION_PROVIDERS, req)
        system_prompt = build_system_prompt(_load_personality(), ContextType.DISCORD_MENTION, context)
        trace.add(system_prompt=system_prompt, user_message=user_message)

        log.debug("ask_berries_discord_mention — user_message: %.120r", user_message)
        response = None
        if AGENT_TOOLS_ENABLED:
            # Experimental tool-use loop (search_memories, get_server_rules, ...).
            # Falls back to the plain single-shot call below if unavailable.
            from shared.agent import run_tool_loop
            with trace.step("agent_loop"):
                response = await run_tool_loop(system_prompt=system_prompt, user_message=user_message, max_tokens=600)
        if response is None:
            with trace.step("llm_response"):
                response = await ask_berries(system_prompt=system_prompt, user_message=user_message, max_tokens=600)
        response = cleanup_response(response) if response else response
        trace.add(response=response)
        log.debug("ask_berries_discord_mention — response: %.120r", response)

        if response:
            from shared.user_db import get_twitch_link
            with trace.step("log_interaction"):
                user_key = await asyncio.to_thread(get_twitch_link, discord_id) or discord_id
                log_interaction(
                    user_key=user_key,
                    nickname=nickname,
                    user_message=query,
                    berries_response=response,
                )
        return response


async def ask_berries_twitch_going_live(
    stream_title: str,
    stream_category: str,
) -> tuple[str, str] | None:
    """
    Going-live announcement pipeline. Returns (announcement, gif_query) or None on failure.

    Makes two sequential LLM calls:
      1. Twig asks Berries to write a going-live Discord announcement → announcement text
      2. Berries picks a Giphy search query to accompany the announcement → gif_query string
    """

    # Check for empty title/category or malformed Streamer.Bot input (e.g. "%string%")
    # Return early if the input looks invalid, to avoid making LLM calls that are likely to fail or produce low-quality output.
    # We have a reputation to uphold, after all!
    if not stream_title.strip() or not stream_category.strip() or re.match(r"^%[^%]+%$", stream_title) or re.match(r"^%[^%]+%$", stream_category):
        log.warning(
            f"ask_berries_twitch_going_live — stream_title or stream_category is empty or malformed. "
            f"Received title: {stream_title!r}, category: {stream_category!r}",
        )
        return None

    with trace.trace("going_live", stream_title=stream_title, stream_category=stream_category):
        return await _going_live_inner(stream_title, stream_category)


async def _going_live_inner(stream_title: str, stream_category: str) -> tuple[str, str] | None:
    system = build_system_prompt(_load_personality(), ContextType.DISCORD_ANNOUNCE)

    user_msg = (
        f"[Twig]: Hey Berries, I just went live! Stream title: '{stream_title}', "
        f"category: '{stream_category}'. Can you write a short (2-3 sentence) friendly going-live announcement for the "
        f"Discord server that tells people what's to expect from the stream, your silly or snarky "
        f"personal opinions on the stream title/category, and then tell people that they're welcome to "
        f"join if that sounds like a good time to them? Don't pressure people, just let them know what's "
        f"happening and tell them they're welcome to join. "
        f"Please write your message for the audience, as your response will be posted verbatim."
    )

    trace.add(system_prompt=system, user_message=user_msg)
    log.debug("ask_berries_twitch_going_live — requesting announcement for %r / %r", stream_title, stream_category)
    with trace.step("llm_announcement"):
        announcement = await ask_berries(system_prompt=system, user_message=user_msg, max_tokens=400, purpose="going_live_announcement")
    if not announcement:
        log.warning("ask_berries_twitch_going_live — LLM returned empty announcement")
        return None
    announcement = cleanup_response(announcement)
    trace.add(response=announcement)

    gif_prompt = (
        "Great! Now generate a Giphy search query for a gif that fits the vibe of your announcement. "
        "Reply with ONLY the search query, 2-5 words, no punctuation, no explanation."
    )
    with trace.step("llm_gif_query"):
        gif_query = await ask_berries(
            system_prompt=system,
            messages=[
                {"role": "user", "content": user_msg},
                {"role": "assistant", "content": announcement},
                {"role": "user", "content": gif_prompt},
            ],
            max_tokens=32,
            purpose="gif_query",
        )
    gif_query = (gif_query or "").strip()
    trace.add(gif_query=gif_query)
    log.debug("ask_berries_twitch_going_live — gif_query: %r", gif_query)

    return announcement, gif_query
