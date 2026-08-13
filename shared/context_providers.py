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
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

from shared import trace
from shared.chroma_client import query_lore_multi
from shared.prompt_builder import (
    format_chroma_context,
    format_lore,
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
    lore_context: str = ""          # recent conversation for the lore query, with Berries' own messages excluded


@dataclass
class ContextBlock:
    """
    One provider's rendered context, optionally re-renderable at a smaller size.

    A provider that can give up material gracefully supplies `parts` (its
    droppable units, ordered most-valuable-first) and `render` (rebuilds the
    block text from any prefix of them). shared/budget.py uses that to shed the
    weakest chunks when the assembled prompt overruns the context ceiling.

    Providers with nothing to give up return a plain string and are normalised
    into a bare ContextBlock: the budget can still drop the block whole, it
    just cannot shrink it. That is why `parts` is not simply the block text
    split on its separator -- chunk text is arbitrary transcript content and
    may contain the separator itself, so the structure is carried through
    rather than recovered by string surgery.
    """

    text: str
    parts: list = field(default_factory=list)
    render: Callable[[list], str] | None = None

    @property
    def shrinkable(self) -> bool:
        return self.render is not None and bool(self.parts)

    def resized(self, parts: list) -> "ContextBlock":
        """A copy holding only `parts`, with `text` re-rendered to match."""
        if self.render is None:
            raise ValueError("ContextBlock is not shrinkable")
        return ContextBlock(text=self.render(parts), parts=list(parts), render=self.render)


class ContextProvider(Protocol):
    name: str
    role: str = "developer"  # "system" merges into the system prompt; "developer" becomes its own message
    async def provide(self, req: BerriesRequest) -> "str | ContextBlock | None": ...


class LoreProvider:
    """
    Curated character facts (berries_bot/lore/facts.md), retrieved from the
    dedicated lore-only ChromaDB collection.

    Recall-oriented on purpose. When lore competed with ~9k transcript chunks
    for the shared retrieval slots, entries reached the prompt for only about
    half the questions they answered — and on a miss the model does not
    deflect, it invents a confident answer (3/6 vs 5/6 on the fabrication
    check; berries_bot/lore/README.md, 2026-07-15). So lore gets its own
    collection, a generous top-n, a lenient distance threshold, and no
    reranking: an irrelevant-but-true fact in the prompt is cheap, a missing
    fact becomes a fabrication.

    Queries are the raw message plus recent conversation, unrewritten — the
    assist-model rewrite earns its keep finding needles in ~9k noisy chunks;
    casting a wide net over ~20 curated entries doesn't need it. The
    conversation query is `lore_context`, not `recent_context`: Berries' own
    messages are excluded, because embedding his replies steers lore retrieval
    toward whatever devices he has already been leaning on (his mushroom-heavy
    voice kept re-retrieving mushroom lore — a feedback loop).

    Runs first so personality + facts lead the prompt.
    """

    name = "lore"
    role = "system"  # character facts belong beside the personality, not as a developer message

    async def provide(self, req: BerriesRequest) -> str | None:
        queries = [q for q in (req.query, req.lore_context) if q.strip()]
        if not queries:
            return None
        try:
            docs = await asyncio.to_thread(query_lore_multi, queries)
        except Exception:
            log.exception("Lore retrieval failed (no character facts injected)")
            return None
        if not docs:
            return None
        trace.add(lore_injected=[meta.get("title", "?") for _doc, meta in docs])
        return format_lore(docs)


class ChromaContextProvider:
    """Long-term memory: reranked ChromaDB retrieval over past transcripts."""

    name = "chroma"

    async def provide(self, req: BerriesRequest) -> ContextBlock | None:
        docs, _queries = await retrieve_context(
            req.query,
            recent_context=req.recent_context,
            username=req.display_name or "a viewer",
        )
        if not docs:
            return None
        # Shrinkable: docs arrive rerank-ordered (most relevant first), so the
        # budget sheds from the tail when the prompt overruns the ceiling.
        return ContextBlock(
            text=format_chroma_context(docs),
            parts=docs,
            render=format_chroma_context,
        )


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


async def build_context(
    providers: list[ContextProvider],
    req: BerriesRequest,
) -> tuple[str, list[ContextBlock]]:
    """
    Run providers in order, split by role.

    Returns (system_context, developer_blocks): system_context is the joined
    text of role="system" providers (currently just LoreProvider — character
    facts belong beside the personality); developer_blocks is the ordered list
    of every other provider's ContextBlock, one per source, sent as separate
    "developer"-role messages downstream (see shared/llm_client.py).

    Providers may return a plain string or a ContextBlock; strings are wrapped
    so callers only ever handle ContextBlocks.
    """
    system_parts: list[str] = []
    developer_blocks: list[ContextBlock] = []
    for provider in providers:
        name = getattr(provider, "name", type(provider).__name__)
        with trace.step(f"context_{name}") as s:
            block = await provider.provide(req)
            if isinstance(block, str):
                block = ContextBlock(text=block)
            s["chars"] = len(block.text) if block else 0
        if not block or not block.text:
            continue
        if getattr(provider, "role", "developer") == "system":
            system_parts.append(block.text)
        else:
            developer_blocks.append(block)
    return "\n\n".join(system_parts), developer_blocks
