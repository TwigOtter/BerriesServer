"""
shared/context_providers.py

Composable system-prompt context blocks.

Each provider turns one source of context (retrieval, user profile, short-term
memory, channel history) into a formatted block for the system prompt — or
None when it has nothing to contribute. The response pipelines in
ask_berries.py compose a list of providers per platform; new context sources
(lore, server rules, tool results, ...) slot in as new providers without
touching the pipelines themselves.
"""

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Protocol

from shared import trace
from shared.config import LORE_FACTS_FILE
from shared.prompt_builder import (
    format_chroma_context,
    format_lore,
    format_recent_chunks,
    format_user_context,
)
from shared.retrieval import retrieve_context

log = logging.getLogger(__name__)


@dataclass
class BerriesRequest:
    """Everything a context provider might need to know about one request."""

    query: str                      # raw user message (drives retrieval + logging)
    display_name: str = ""          # what to call the user (nickname or platform name)
    t_login: str | None = None      # Twitch login, for user_db lookups
    discord_id: str | None = None   # Discord snowflake, for user_db lookups
    recent_context: str = ""        # free-text recency context for query rewriting
    recent_chunks: list[str] = field(default_factory=list)  # flushed chunk texts
    channel_history: str = ""       # pre-formatted Discord channel history block


class ContextProvider(Protocol):
    name: str
    async def provide(self, req: BerriesRequest) -> str | None: ...


class LoreProvider:
    """
    Curated character facts (berries_bot/lore/facts.md), injected verbatim into
    every personality prompt.

    Not retrieved: the whole file is ~2k tokens, small enough to always carry,
    and retrieval was the failure mode. Measured against the live index, the
    ~20 lore entries reached the prompt for only about half of the questions
    they answered — they lose the similarity contest against ~9k transcript
    chunks. On a miss the model does not deflect (personality.txt's "be
    spookily vague" rule only fires for things Berries *wouldn't* know — his
    own bandana isn't one), it invents a confident answer instead. Always
    injecting took a 6-question fabrication check from 3/6 accurate to 5/6.

    Runs first so personality + facts form one stable prefix ahead of the
    per-request blocks.
    """

    name = "lore"

    async def provide(self, req: BerriesRequest) -> str | None:
        if not LORE_FACTS_FILE.exists():
            log.warning("lore facts file not found (%s) — no character facts injected", LORE_FACTS_FILE)
            return None
        text = LORE_FACTS_FILE.read_text(encoding="utf-8").strip()
        return format_lore(text) if text else None


class ChromaContextProvider:
    """Long-term memory: reranked ChromaDB retrieval over past transcripts."""

    name = "chroma"

    async def provide(self, req: BerriesRequest) -> str | None:
        docs, _queries = await retrieve_context(
            req.query,
            recent_context=req.recent_context,
            username=req.display_name or "a viewer",
        )
        return format_chroma_context(docs) if docs else None


class UserProfileProvider:
    """USER PROFILE block from user_db, looked up by t_login or discord_id."""

    name = "user_profile"

    async def provide(self, req: BerriesRequest) -> str | None:
        user = await asyncio.to_thread(self._lookup, req)
        if not user:
            return None
        return format_user_context(user, req.display_name) or None

    @staticmethod
    def _lookup(req: BerriesRequest) -> dict | None:
        from shared.user_db import get_twitch_link, get_user, get_user_by_discord
        if req.t_login:
            return get_user(req.t_login)
        if req.discord_id:
            t_login = get_twitch_link(req.discord_id)
            return get_user(t_login) if t_login else get_user_by_discord(req.discord_id)
        return None


class RecentChunksProvider:
    """Short-term memory: recently flushed chunks from the live session."""

    name = "recent_chunks"

    async def provide(self, req: BerriesRequest) -> str | None:
        return format_recent_chunks(req.recent_chunks) if req.recent_chunks else None


class ChannelHistoryProvider:
    """Pre-formatted Discord channel history (fetched by the bot)."""

    name = "channel_history"

    async def provide(self, req: BerriesRequest) -> str | None:
        return req.channel_history or None


async def build_context(
    providers: list[ContextProvider],
    req: BerriesRequest,
) -> str:
    """Run providers in order and join the non-empty blocks."""
    parts: list[str] = []
    for provider in providers:
        name = getattr(provider, "name", type(provider).__name__)
        with trace.step(f"context_{name}") as s:
            block = await provider.provide(req)
            s["chars"] = len(block) if block else 0
        if block:
            parts.append(block)
    return "\n\n".join(parts)
