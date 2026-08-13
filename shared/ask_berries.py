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
from shared.budget import fit_to_budget
from shared.config import (
    AGENT_TOOLS_ENABLED,
    DISCORD_HISTORY_TOKEN_LIMIT,
    DISCORD_RESPONSE_TOKENS,
    PERSONALITY_FILE,
    TWITCH_HISTORY_TOKEN_LIMIT,
    TWITCH_RESPONSE_TOKENS,
)
from shared.context_providers import (
    BerriesRequest,
    ChromaContextProvider,
    ContextBlock,
    LoreProvider,
    UserProfileProvider,
    build_context,
)
from shared.history import HistoryItem, build_history_turns, format_user_turn
from shared.interactions_db import get_recent_twitch_messages
from shared.llm_client import get_completion
from shared.prompt_builder import ContextType, build_system_prompt
from shared.interaction_log import log_interaction

log = logging.getLogger(__name__)

# Context blocks per platform, in prompt order. Adding a new context source
# (server rules, tool results, ...) means adding a provider here, not a new
# pipeline.
#
# LoreProvider leads on both platforms so personality + character facts open
# the prompt the same way wherever Berries is invoked. It retrieves from the
# dedicated lore collection (recall-oriented, no rerank) — see the provider's
# docstring and berries_bot/lore/README.md for why lore does not share the
# transcript retrieval pool.
#
# Conversation history and the user-profile block are NOT in these lists —
# history turns are spliced in separately (build_history_turns) and the
# profile block is placed right before the final query, not up front with
# these "lead" blocks. See ask_berries_twitch/ask_berries_discord_mention.
_TWITCH_LEAD_PROVIDERS = [
    LoreProvider(),
    ChromaContextProvider(),
]
_DISCORD_LEAD_PROVIDERS = [
    LoreProvider(),
    ChromaContextProvider(),
]
_PROFILE_PROVIDER = UserProfileProvider()


def _assemble_messages(
    lead_blocks: list[ContextBlock],
    history_turns: list[dict],
    profile_block: str | None,
    final_query: str,
) -> list[dict]:
    """
    [developer(lead context: lore retrieval, ...), *history turns,
    developer(user profile — right before the query it's about), user(final query)]
    """
    messages = [{"role": "developer", "content": b.text} for b in lead_blocks]
    messages += history_turns
    if profile_block:
        messages.append({"role": "developer", "content": profile_block})
    messages.append({"role": "user", "content": final_query})
    return messages


def _fit(
    system_prompt: str,
    lead_blocks: list[ContextBlock],
    history_turns: list[dict],
    profile_block: str | None,
    final_query: str,
    max_tokens: int,
) -> tuple[list[dict], list[ContextBlock], list[dict]]:
    """
    Trim context to the backend's context ceiling.

    Returns (messages, lead_blocks, history_turns) — the assembled message list
    plus the trimmed pieces it was built from, since the agent path composes
    those pieces differently and must not fall back to the untrimmed ones.

    The per-source token limits (history, windowing, lore) each cap one input;
    this caps their sum, which is what actually has to fit. Overruns used to
    reach the backend and come back as a 400 with no response at all — see
    shared/budget.py.
    """
    with trace.step("prompt_budget"):
        lead_blocks, history_turns = fit_to_budget(
            system_prompt=system_prompt,
            lead_blocks=lead_blocks,
            history_turns=history_turns,
            profile_block=profile_block,
            final_query=final_query,
            max_tokens=max_tokens,
            assemble=_assemble_messages,
        )
    messages = _assemble_messages(lead_blocks, history_turns, profile_block, final_query)
    return messages, lead_blocks, history_turns


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

    messages: full conversation, possibly including role="developer" context
              blocks at specific positions. When provided, takes precedence
              over user_message. See shared/llm_client.py.
    purpose:  label for the call in logs/traces (see shared/llm_client.py).
    """
    return await get_completion(
        system_prompt=system_prompt, user_message=user_message,
        max_tokens=max_tokens, messages=messages,
        purpose=purpose,
    )


async def ask_berries_discord(
    user_message: str,
    context_type: ContextType = ContextType.DISCORD_MENTION,
    context: str = "",
    max_tokens: int = DISCORD_RESPONSE_TOKENS,
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
        recent_buffer_text: Last N in-progress buffer entries for query rewriting context
                             (NOT conversation history — see get_recent_twitch_messages
                             for that).
    """
    with trace.trace("twitch_mention", username=username, query=query, tts=tts):
        with trace.step("nickname_lookup"):
            nickname = await asyncio.to_thread(_get_nickname_twitch, username) if username else ""
        final_query = format_user_turn(nickname, query) if username else query

        context_type = ContextType.TWITCH_TTS if tts else ContextType.TWITCH_CHAT

        req = BerriesRequest(
            query=query,
            display_name=nickname or username or "a viewer",
            t_login=username or None,
            recent_context=recent_buffer_text,
            # Twitch buffer text is multi-user chat, not dominated by Berries'
            # own replies, so it serves as the lore query unfiltered.
            lore_context=recent_buffer_text,
        )
        system_context, lead_blocks = await build_context(_TWITCH_LEAD_PROVIDERS, req)

        with trace.step("recent_twitch_messages") as s:
            rows = await asyncio.to_thread(get_recent_twitch_messages)
            s["rows"] = len(rows)
        items: list[HistoryItem] = [
            ("assistant" if row["is_bot"] else "user", row["display_name"], row["content"])
            for row in rows
        ]
        history_turns = build_history_turns(items, token_limit=TWITCH_HISTORY_TOKEN_LIMIT)

        with trace.step("context_user_profile") as s:
            profile_block = await _PROFILE_PROVIDER.provide(req)
            s["chars"] = len(profile_block) if profile_block else 0
        system_prompt = build_system_prompt(_load_personality(), context_type, system_context)
        messages, _lead_blocks, _history_turns = _fit(
            system_prompt, lead_blocks, history_turns, profile_block, final_query,
            max_tokens=TWITCH_RESPONSE_TOKENS,
        )
        trace.add(system_prompt=system_prompt, messages=messages)
        with trace.step("llm_response"):
            response = await ask_berries(
                system_prompt=system_prompt, messages=messages,
                max_tokens=TWITCH_RESPONSE_TOKENS,
            )
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
    history_items: list[HistoryItem],
    recent_context_text: str = "",
    recent_user_messages: str = "",
) -> str | None:
    """
    Full Discord @mention pipeline.

    Looks up nickname from user_db internally (via discord_id), queries ChromaDB,
    assembles the system prompt, calls the LLM, cleans the response, and logs.

    Args:
        query:                Raw message content (with @mention token already replaced).
        display_name:         Discord display name shown in the user_message to Berries.
        discord_id:           Discord user ID string; used for nickname lookup in user_db.
        history_items:        (role, display_name, text) tuples from the channel, oldest
                              -> newest (from discord_bot/cogs/mention.py::_get_channel_history).
        recent_context_text:  All fetched channel lines, unbudgeted — feeds retrieval
                              query rewriting (recent_context), not conversation history.
        recent_user_messages: Recent channel messages with Berries' own excluded;
                              drives the lore query so his voice doesn't steer lore retrieval.
    """
    with trace.trace("discord_mention", username=display_name, discord_id=discord_id, query=query):
        with trace.step("nickname_lookup"):
            nickname = await asyncio.to_thread(_get_nickname_discord, discord_id, display_name)
        final_query = format_user_turn(nickname, query)

        req = BerriesRequest(
            query=query,
            display_name=display_name,
            discord_id=discord_id,
            recent_context=recent_context_text,
            lore_context=recent_user_messages,
        )
        system_context, lead_blocks = await build_context(_DISCORD_LEAD_PROVIDERS, req)
        history_turns = build_history_turns(history_items, token_limit=DISCORD_HISTORY_TOKEN_LIMIT)
        with trace.step("context_user_profile") as s:
            profile_block = await _PROFILE_PROVIDER.provide(req)
            s["chars"] = len(profile_block) if profile_block else 0
        system_prompt = build_system_prompt(_load_personality(), ContextType.DISCORD_MENTION, system_context)
        messages, lead_blocks, history_turns = _fit(
            system_prompt, lead_blocks, history_turns, profile_block, final_query,
            max_tokens=DISCORD_RESPONSE_TOKENS,
        )
        trace.add(system_prompt=system_prompt, messages=messages)

        log.debug("ask_berries_discord_mention — final_query: %.120r", final_query)
        response = None
        if AGENT_TOOLS_ENABLED:
            # Experimental tool-use loop (search_memories, get_server_rules, ...).
            # Falls back to the plain single-shot call below if unavailable.
            # Doesn't (yet) have turn-history awareness, so history is flattened
            # into one more developer block, preserving today's behavior for
            # this off-by-default path. See docs/agent-tools.md.
            from shared.agent import run_tool_loop
            agent_dev_blocks = [b.text for b in lead_blocks]
            if profile_block:
                agent_dev_blocks.append(profile_block)
            if history_turns:
                agent_dev_blocks.append("\n".join(t["content"] for t in history_turns))
            with trace.step("agent_loop"):
                response = await run_tool_loop(
                    system_prompt=system_prompt, user_message=final_query,
                    max_tokens=DISCORD_RESPONSE_TOKENS, developer_blocks=agent_dev_blocks,
                )
        if response is None:
            with trace.step("llm_response"):
                response = await ask_berries(
                    system_prompt=system_prompt, messages=messages,
                    max_tokens=DISCORD_RESPONSE_TOKENS,
                )
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
