"""
discord_bot/cogs/profile.py

User profile slash commands: /ping, /twitch-link, /set-nickname, /about-me,
/set-pronouns, /set-species, /set-birthday, /set-timezone.
"""

import logging
import re
from datetime import datetime
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands

from shared.config import DISCORD_LOG_CHANNEL_ID
from shared.user_db import (
    get_discord_for_twitch,
    get_twitch_link,
    get_user,
    get_user_by_discord,
    link_discord,
    set_birthday,
    set_nickname,
    set_nickname_for_discord,
    set_pronouns,
    set_species,
    set_timezone,
    upsert_discord_user,
)

log = logging.getLogger("discord_bot.profile")


class ProfileCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def _send_to_log_channel(self, message: str) -> None:
        if not DISCORD_LOG_CHANNEL_ID:
            return
        try:
            log_channel = self.bot.get_channel(DISCORD_LOG_CHANNEL_ID) or await self.bot.fetch_channel(DISCORD_LOG_CHANNEL_ID)
            await log_channel.send(message)
        except Exception as e:
            log.warning("Failed to send to log channel %s: %s", DISCORD_LOG_CHANNEL_ID, e)

    @app_commands.command(name="ping", description="Check if Berries is lurking")
    async def ping(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message("*stares from the shadows* ...yes, I am here. :3")

    @app_commands.command(name="twitch-link", description="Link your Twitch account to your Discord profile")
    @app_commands.describe(twitch_username="Your Twitch username (without the @)")
    async def twitch_link(self, interaction: discord.Interaction, twitch_username: str) -> None:
        await interaction.response.defer(ephemeral=True)

        twitch_username = twitch_username.lstrip("@").strip().lower()
        if not re.fullmatch(r"[a-z0-9_]{4,25}", twitch_username):
            await interaction.followup.send(
                "*tilts head* ...that doesn't look like a valid Twitch username. "
                "Twitch usernames are 4–25 characters and only contain letters, numbers, and underscores.",
                ephemeral=True,
            )
            return

        discord_id = str(interaction.user.id)

        # Block if the Twitch account is already claimed by a different Discord user
        existing_discord = get_discord_for_twitch(twitch_username)
        if existing_discord and existing_discord != discord_id:
            await interaction.followup.send(
                f"*narrows eyes from the shadows* ...Twitch account **{twitch_username}** is already linked to a different Discord account. "
                "If you believe this is an error, please contact a moderator.",
                ephemeral=True,
            )
            log.warning(
                "Twitch link BLOCKED: Discord user %s (%s) tried to claim Twitch %r, already owned by Discord ID %s",
                interaction.user, discord_id, twitch_username, existing_discord,
            )
            return

        result = link_discord(twitch_username, discord_id, d_username=interaction.user.name)
        status = result["status"]
        previous = result.get("previous")

        if status == "already_linked":
            await interaction.followup.send(
                f"*peers at you* ...your Discord is already linked to **{twitch_username}** on Twitch. Nothing to change!",
                ephemeral=True,
            )
        elif previous and previous != twitch_username:
            await interaction.followup.send(
                f"*rustles thoughtfully* ...updated your link from **{previous}** to **{twitch_username}**. Got it.",
                ephemeral=True,
            )
        else:
            await interaction.followup.send(
                f"*nods slowly from the dark* ...noted. Your Discord is now linked to Twitch account **{twitch_username}**.",
                ephemeral=True,
            )

        log.info(
            "Twitch link: Discord user %s (%s) → Twitch %r (status=%s, previous=%r)",
            interaction.user, discord_id, twitch_username, status, previous,
        )

        if status != "already_linked":
            detail = f" (was `{previous}`)" if previous else ""
            await self._send_to_log_channel(
                f"**Twitch link** | {interaction.user.mention} (`{interaction.user}`) "
                f"→ `{twitch_username}` | status: `{status}`{detail}"
            )

    @app_commands.command(name="set-nickname", description="Set the nickname Berries uses for you")
    @app_commands.describe(nickname="What you'd like Berries to call you (max 32 characters)")
    async def set_nickname_cmd(self, interaction: discord.Interaction, nickname: str) -> None:
        await interaction.response.defer(ephemeral=True)

        discord_id = str(interaction.user.id)
        t_login = get_twitch_link(discord_id)

        nickname = nickname.strip()
        if not nickname:
            await interaction.followup.send(
                "*blinks slowly* ...you have to actually give me a name to call you.",
                ephemeral=True,
            )
            return

        if len(nickname) > 32:
            await interaction.followup.send(
                "*squints* ...that's a bit long. Keep it under 32 characters.",
                ephemeral=True,
            )
            return

        if t_login:
            set_nickname(t_login, nickname)
        else:
            upsert_discord_user(discord_id, d_username=interaction.user.name)
            set_nickname_for_discord(discord_id, nickname)

        log.info(
            "Nickname set: Discord user %s (%s) / Twitch %r → %r",
            interaction.user, discord_id, t_login, nickname,
        )

        await interaction.followup.send(
            f"*rustles quietly* ...understood. I'll call you **{nickname}** from now on.",
            ephemeral=True,
        )

        await self._send_to_log_channel(
            f"**Nickname set** | {interaction.user.mention} (`{interaction.user}`) "
            f"/ Twitch `{t_login}` → `{nickname}`"
        )

    @app_commands.command(name="about-me", description="See what Berries knows about you")
    async def about_me_cmd(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)

        discord_id = str(interaction.user.id)
        t_login = get_twitch_link(discord_id)
        user = get_user(t_login) if t_login else get_user_by_discord(discord_id)

        if not user:
            await interaction.followup.send(
                "*peers into the shadows* ...I don't have a profile for you yet. "
                "Try `/twitch-link` to connect your Twitch account, or use any of the `/set-*` commands to get started.",
                ephemeral=True,
            )
            return

        name = user.get("nickname") or user.get("t_login") or user.get("d_username") or interaction.user.display_name

        lines = ["**USER PROFILE**"]
        lines.append(f"**Name:** {name}")
        lines.append(f"**Pronouns:** {user['pronouns'] if user.get('pronouns') else '*not set — use `/set-pronouns`*'}")
        lines.append(f"**Species:** {user['species'] if user.get('species') else '*not set — use `/set-species`*'}")

        tz = user.get("timezone")
        if tz:
            try:
                local_time = datetime.now(ZoneInfo(tz)).strftime("%A %H:%M %Z")
                lines.append(f"**Local time:** {local_time} *(timezone: {tz})*")
            except Exception:
                lines.append(f"**Timezone:** {tz} *(invalid — use `/set-timezone` to fix)*")
        else:
            lines.append("**Timezone:** *not set — use `/set-timezone`*")

        if user.get("birthday"):
            lines.append(f"**Birthday:** {user['birthday']}")
        else:
            lines.append("**Birthday:** *not set — use `/set-birthday`*")

        if user.get("about"):
            lines.append(f"**About:** {user['about']}")
        else:
            lines.append("**About:** *nothing yet — Berries will fill this in when he dreams after your first conversation :3*")

        if t_login:
            lines.append(f"\n*Twitch: `{t_login}`*")

        await interaction.followup.send("\n".join(lines), ephemeral=True)

    @app_commands.command(name="set-pronouns", description="Set the pronouns Berries uses for you")
    @app_commands.describe(pronouns="Your pronouns (e.g. she/her, he/him, they/them)")
    async def set_pronouns_cmd(self, interaction: discord.Interaction, pronouns: str) -> None:
        await interaction.response.defer(ephemeral=True)

        discord_id = str(interaction.user.id)
        pronouns = pronouns.strip()
        if not pronouns:
            await interaction.followup.send(
                "*blinks* ...you have to actually give me something to go on.",
                ephemeral=True,
            )
            return
        if len(pronouns) > 32:
            await interaction.followup.send(
                "*squints* ...that's a lot of pronouns. Keep it under 32 characters.",
                ephemeral=True,
            )
            return

        upsert_discord_user(discord_id, d_username=interaction.user.name)
        set_pronouns(discord_id, pronouns)

        log.info("Pronouns set: Discord user %s (%s) → %r", interaction.user, discord_id, pronouns)
        await interaction.followup.send(
            f"*nods* ...noted. I'll use **{pronouns}** for you.",
            ephemeral=True,
        )

    @app_commands.command(name="set-species", description="Set your primary fursona species")
    @app_commands.describe(species="Your fursona species (e.g. red fox, border collie)")
    async def set_species_cmd(self, interaction: discord.Interaction, species: str) -> None:
        await interaction.response.defer(ephemeral=True)

        discord_id = str(interaction.user.id)
        species = species.strip()
        if not species:
            await interaction.followup.send(
                "*blinks* ...you have to actually tell me what you are.",
                ephemeral=True,
            )
            return
        if len(species) > 64:
            await interaction.followup.send(
                "*squints* ...that's a mouthful. Keep it under 64 characters.",
                ephemeral=True,
            )
            return

        upsert_discord_user(discord_id, d_username=interaction.user.name)
        set_species(discord_id, species)

        log.info("Species set: Discord user %s (%s) → %r", interaction.user, discord_id, species)
        await interaction.followup.send(
            f"*tilts head* ...noted. I'll remember that you're a **{species}**.",
            ephemeral=True,
        )

    @app_commands.command(name="set-birthday", description="Set your birthday (month and day, no year stored)")
    @app_commands.describe(month="Birth month (1–12)", day="Birth day (1–31)")
    async def set_birthday_cmd(
        self,
        interaction: discord.Interaction,
        month: app_commands.Range[int, 1, 12],
        day: app_commands.Range[int, 1, 31],
    ) -> None:
        await interaction.response.defer(ephemeral=True)

        discord_id = str(interaction.user.id)
        birthday = f"{month:02d}-{day:02d}"

        upsert_discord_user(discord_id, d_username=interaction.user.name)
        set_birthday(discord_id, birthday)

        log.info("Birthday set: Discord user %s (%s) → %s", interaction.user, discord_id, birthday)
        await interaction.followup.send(
            f"*rustles quietly* ...I'll remember **{birthday}**. No year, just the day — as it should be.",
            ephemeral=True,
        )

    @app_commands.command(name="set-timezone", description="Set your timezone so Berries knows what time it is for you")
    @app_commands.describe(timezone="IANA timezone name (e.g. America/New_York, Europe/London, Asia/Tokyo)")
    async def set_timezone_cmd(self, interaction: discord.Interaction, timezone: str) -> None:
        await interaction.response.defer(ephemeral=True)

        timezone = timezone.strip()
        try:
            ZoneInfo(timezone)
        except Exception:
            await interaction.followup.send(
                f"*narrows eyes* ...I don't recognize **{timezone}** as a valid timezone. "
                "Use an IANA name like `America/New_York`, `Europe/London`, or `Asia/Tokyo`.",
                ephemeral=True,
            )
            return

        discord_id = str(interaction.user.id)
        upsert_discord_user(discord_id, d_username=interaction.user.name)
        set_timezone(discord_id, timezone)

        log.info("Timezone set: Discord user %s (%s) → %r", interaction.user, discord_id, timezone)
        await interaction.followup.send(
            f"*nods slowly* ...noted. Your timezone is set to **{timezone}**.",
            ephemeral=True,
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ProfileCog(bot))
