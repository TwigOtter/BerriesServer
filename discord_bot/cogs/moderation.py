"""
discord_bot/cogs/moderation.py

Stickers-only channel enforcement: non-sticker messages from non-mods in
DISCORD_STICKERS_ONLY_CHANNEL_IDS channels are deleted and answered with the
rules sticker.
"""

import logging

import discord
from discord.ext import commands

from shared.config import (
    DISCORD_RULES_STICKER_ID,
    DISCORD_STICKERS_ONLY_CHANNEL_IDS,
)

log = logging.getLogger("discord_bot.moderation")


class ModerationCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        # Cached GuildSticker object so we don't fetch it on every violation.
        self._rules_sticker_cache: discord.GuildSticker | None = None

    async def _get_rules_sticker(self, guild: discord.Guild) -> discord.GuildSticker | None:
        if self._rules_sticker_cache is not None:
            return self._rules_sticker_cache
        if not DISCORD_RULES_STICKER_ID:
            return None
        try:
            self._rules_sticker_cache = await guild.fetch_sticker(DISCORD_RULES_STICKER_ID)
        except Exception:
            log.exception("Failed to fetch rules sticker %s", DISCORD_RULES_STICKER_ID)
        return self._rules_sticker_cache

    @commands.Cog.listener("on_message")
    async def enforce_stickers_only(self, message: discord.Message) -> None:
        if message.author == self.bot.user:
            return
        if not DISCORD_STICKERS_ONLY_CHANNEL_IDS or message.channel.id not in DISCORD_STICKERS_ONLY_CHANNEL_IDS:
            return

        member = message.author if isinstance(message.author, discord.Member) else None
        is_mod = member is not None and member.guild_permissions.manage_messages
        if is_mod or message.stickers:
            return

        log.info(
            "Deleting non-sticker message from %s in stickers-only channel %s",
            message.author, message.channel.id,
        )
        try:
            await message.delete()
        except discord.Forbidden:
            log.warning("Missing permissions to delete message in channel %s", message.channel.id)
        except Exception:
            log.exception("Failed to delete message in channel %s", message.channel.id)
        sticker = await self._get_rules_sticker(message.guild)
        if sticker:
            try:
                await message.channel.send(stickers=[sticker])
            except Exception:
                log.exception("Failed to send rules sticker in channel %s", message.channel.id)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ModerationCog(bot))
