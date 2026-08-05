"""
discord_bot/cogs/mention.py

@mention responses: when Berries is mentioned, fetch channel history, run the
RAG-backed Discord mention pipeline, and reply in-channel. Includes the
redirect-to-berries-chat policy for non-whitelisted channels.
"""

import asyncio
import logging
import time
from contextlib import asynccontextmanager

import discord
from discord.ext import commands

from discord_bot.utils import message_row, resolve_discord_tags
from shared.ask_berries import ask_berries_discord_mention
from shared.config import (
    DISCORD_BERRIES_CHANNEL_WHITELIST_IDS,
    DISCORD_BERRIES_CHAT_CHANNEL_ID,
    DISCORD_STICKERS_ONLY_CHANNEL_IDS,
    DISCORD_CHANNEL_INTERACTION_LIMIT,
)
from shared.history import HistoryItem
from shared.interactions_db import log_discord_message

log = logging.getLogger("discord_bot.mention")


@asynccontextmanager
async def _maybe_typing(channel: discord.abc.Messageable):
    """
    Show the typing indicator if Discord allows it; if Discord rejects the
    request (e.g. 429 rate-limit on the typing endpoint), proceed silently
    so the response itself still goes out. Typing is purely cosmetic and
    must never gate message delivery.
    """
    cm = channel.typing()
    started = False
    try:
        await cm.__aenter__()
        started = True
    except discord.HTTPException as e:
        log.warning("Typing indicator unavailable (%s); responding without it", e)
    try:
        yield
    finally:
        if started:
            try:
                await cm.__aexit__(None, None, None)
            except Exception:
                pass


class MentionCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def _get_channel_history(
        self,
        channel: discord.TextChannel,
        before: discord.Message,
        limit: int = 20,
        user_lines_limit: int = 6,
    ) -> tuple[list[HistoryItem], str, str]:
        """
        Fetch the last `limit` messages from `channel` before `message`.

        Returns (history_items, recent_context_text, recent_user_messages):
          history_items:        (role, display_name, text) tuples, oldest -> newest,
                                ready for shared.history.build_history_turns(). role is
                                "assistant" for Berries' own messages, "user" otherwise
                                -- token-budget trimming happens downstream, not here.
          recent_context_text:  all fetched lines, unbudgeted -- feeds retrieval query
                                rewriting (recent_context), not conversation history.
          recent_user_messages: the last `user_lines_limit` non-bot messages,
                                plain lines. Used as the lore retrieval query —
                                Berries' own messages are excluded so his voice
                                doesn't steer which lore comes back.
        """
        try:
            messages = [m async for m in channel.history(limit=limit, before=before)]
            messages.reverse()
            entries = [
                (m, resolve_discord_tags(m, bot_user=self.bot.user))
                for m in messages
                if m.content
            ]
            history_items: list[HistoryItem] = [
                ("assistant" if m.author == self.bot.user else "user", m.author.display_name, text)
                for m, text in entries
            ]
            lines = [f"{m.author.display_name}: {text}" for m, text in entries]
            user_lines = [
                f"{m.author.display_name}: {text}" for m, text in entries if m.author != self.bot.user
            ][-user_lines_limit:]
            return history_items, "\n".join(lines), "\n".join(user_lines)
        except Exception:
            log.exception("Failed to fetch channel history for channel %s", channel.id)
            return [], "", ""

    async def _count_recent_bot_messages(
        self,
        channel: discord.TextChannel,
        before: discord.Message,
        limit: int = 20,
    ) -> int:
        """Count how many of the last `limit` messages were sent by the bot."""
        try:
            messages = [m async for m in channel.history(limit=limit, before=before)]
            return sum(1 for m in messages if m.author == self.bot.user)
        except Exception:
            log.exception("Failed to count bot messages in channel %s", channel.id)
            return 0

    @commands.Cog.listener("on_message")
    async def respond_to_mention(self, message: discord.Message) -> None:
        if message.author == self.bot.user:
            return
        # Stickers-only channels are handled (and messages deleted) by ModerationCog.
        if DISCORD_STICKERS_ONLY_CHANNEL_IDS and message.channel.id in DISCORD_STICKERS_ONLY_CHANNEL_IDS:
            return

        mentioned = self.bot.user in message.mentions and not message.mention_everyone
        if not mentioned:
            return

        # Resolves the bot's own mention to @BerriesTheDemon and any other
        # user/role/channel/emoji tags to readable names.
        content = resolve_discord_tags(message, bot_user=self.bot.user).strip()
        if not content:
            log.debug("Ignoring empty message from %s in channel %s", message.author, message.channel.id)
            return

        log.info(
            "Responding to %s in channel %s: %.120r",
            message.author, message.channel.id, content,
        )

        # Dual-write (docs/sql-interaction-storage.md Phase 1). If the watcher
        # cog already recorded this message, the upsert only escalates the
        # invoked_berries flag.
        await asyncio.to_thread(
            log_discord_message,
            **message_row(message, bot_user=self.bot.user, invoked_berries=True),
        )

        # If Berries was @mentioned in a non-whitelisted channel and has already spoken
        # twice in the recent history, redirect to #berries-chat instead of responding.
        if (
            DISCORD_BERRIES_CHAT_CHANNEL_ID
            and message.channel.id not in DISCORD_BERRIES_CHANNEL_WHITELIST_IDS
        ):
            bot_count = await self._count_recent_bot_messages(message.channel, before=message)
            if bot_count >= DISCORD_CHANNEL_INTERACTION_LIMIT:
                log.info(
                    "Redirecting %s to berries-chat (%d recent bot messages in channel %s)",
                    message.author, bot_count, message.channel.id,
                )
                await message.channel.send(
                    f"Hey, there's a lot of people here and it's making me anxious to talk here too much. "
                    f"If you want to have a conversation, let's talk in <#{DISCORD_BERRIES_CHAT_CHANNEL_ID}>"
                )
                return

        try:
            t0 = time.perf_counter()
            async with _maybe_typing(message.channel):
                history_items, recent_context_text, recent_user_messages = await self._get_channel_history(
                    message.channel, before=message,
                )
                response = await ask_berries_discord_mention(
                    query=content,
                    display_name=message.author.display_name,
                    discord_id=str(message.author.id),
                    history_items=history_items,
                    recent_context_text=recent_context_text,
                    recent_user_messages=recent_user_messages,
                )
                log.debug("LLM response for mention: %.120r", response)

            sent = await message.channel.send(response)
            # Berries' own reply is part of the record (is_bot=1) — the future
            # chunker and context queries need both sides of the conversation.
            await asyncio.to_thread(
                log_discord_message,
                **message_row(sent, bot_user=self.bot.user),
            )
            log.info(
                "Sent response to %s in channel %s (%.2fs end-to-end)",
                message.author, message.channel.id, time.perf_counter() - t0,
            )
        except Exception:
            log.exception(
                "Failed to generate/send response to %s in channel %s",
                message.author, message.channel.id,
            )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(MentionCog(bot))
