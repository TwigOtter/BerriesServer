"""
discord_bot/cogs/watcher.py

Watch-channel buffering: messages in DISCORD_WATCH_CHANNEL_IDS channels are
buffered per channel and flushed to ChromaDB at CHUNK_TOKEN_LIMIT tokens.

There is deliberately no inactivity flush: Discord conversations are slow and
asynchronous (hours between messages is normal), so time-based chunking would
fragment them.
"""

import asyncio
import logging
import uuid
from datetime import datetime, timezone

import discord
from discord.ext import commands

from discord_bot.utils import resolve_discord_tags
from shared.chroma_client import get_collection
from shared.config import (
    CHUNK_TOKEN_LIMIT,
    DISCORD_CHUNK_OVERLAP_MESSAGES,
    DISCORD_WATCH_CHANNEL_IDS,
)
from shared.tokenizer import count_tokens

log = logging.getLogger("discord_bot.watcher")


class WatcherCog(commands.Cog):
    """Buffers watch-channel messages and embeds chunks into ChromaDB."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        # Each channel gets its own list of {"source", "text", "timestamp"} entries.
        self._buffers: dict[int, list[dict]] = {}

    @commands.Cog.listener("on_message")
    async def buffer_message(self, message: discord.Message) -> None:
        # Runs for all messages including bots and Berries herself —
        # the transcript should contain the whole conversation.
        if not DISCORD_WATCH_CHANNEL_IDS or message.channel.id not in DISCORD_WATCH_CHANNEL_IDS:
            return
        if not message.content:
            return

        channel_id = message.channel.id
        buf = self._buffers.setdefault(channel_id, [])
        # Resolve <@id>-style tags before indexing — chunks are read back by
        # the LLM, which cannot map snowflakes to people.
        buf.append({
            "source": message.author.display_name,
            "text": f"[{message.author.display_name}]: {resolve_discord_tags(message, bot_user=self.bot.user)}",
            "timestamp": message.created_at.timestamp(),
        })
        log.debug("Watch buffer #%s: %d entries", channel_id, len(buf))

        buf_text = "\n".join(e["text"] for e in buf)
        if count_tokens(buf_text) >= CHUNK_TOKEN_LIMIT:
            await self._flush(channel_id, reason="token_limit")

    async def _flush(self, channel_id: int, reason: str) -> None:
        buf = self._buffers.get(channel_id)
        if not buf:
            return

        channel = self.bot.get_channel(channel_id)
        channel_name = getattr(channel, "name", str(channel_id))
        guild_id = str(channel.guild.id) if channel and hasattr(channel, "guild") and channel.guild else ""

        now = datetime.now(timezone.utc)
        chunk_id = f"discord_{now.strftime('%Y-%m-%dT%H-%M-%S')}_{uuid.uuid4().hex[:6]}"
        start_ts = datetime.fromtimestamp(buf[0]["timestamp"], tz=timezone.utc).isoformat()
        end_ts = datetime.fromtimestamp(buf[-1]["timestamp"], tz=timezone.utc).isoformat()
        text = "\n".join(e["text"] for e in buf)
        token_count = count_tokens(text)

        # The embedding round-trip and Chroma client are synchronous — run them
        # off the event loop so a slow embed doesn't stall the bot's heartbeat.
        def _chroma_add() -> None:
            collection = get_collection()
            collection.add(
                documents=[text],
                ids=[chunk_id],
                metadatas=[{
                    "source": "discord",
                    "channel_id": str(channel_id),
                    "channel_name": channel_name,
                    "guild_id": guild_id,
                    "start_time": start_ts,
                    "end_time": end_ts,
                    "flush_reason": reason,
                    "token_count": token_count,
                }],
            )

        try:
            await asyncio.to_thread(_chroma_add)
            log.info(
                "Flushed watch channel #%s (%s): %d entries, %d tokens, reason=%s",
                channel_name, channel_id, len(buf), token_count, reason,
            )
        except Exception:
            log.exception("Failed to embed watch channel chunk for channel %s", channel_id)

        # Keep last DISCORD_CHUNK_OVERLAP_MESSAGES entries as seed for next chunk,
        # but trim from the front if the overlap itself already exceeds the token limit.
        # If even a single entry exceeds the limit (e.g. a long Berries response), clear
        # the overlap entirely to avoid cascading single-message flushes.
        overlap = buf[-DISCORD_CHUNK_OVERLAP_MESSAGES:]
        while len(overlap) > 1 and count_tokens("\n".join(e["text"] for e in overlap)) >= CHUNK_TOKEN_LIMIT:
            overlap = overlap[1:]
        if count_tokens("\n".join(e["text"] for e in overlap)) >= CHUNK_TOKEN_LIMIT:
            overlap = []
        self._buffers[channel_id] = overlap


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(WatcherCog(bot))
