"""
discord_bot/main.py

Entry point for Berries' Discord bot. Wires together:
  - cogs/         — feature modules (mention responses, watch-channel
                    buffering, moderation, /movie commands, profile commands)
  - webhook.py    — FastAPI server (localhost:8002) for going-live events
                    forwarded from ingest_api
  - services.py   — OMDb/Giphy HTTP clients used by the cogs and webhook

Run with:
    python -m discord_bot.main
"""

import asyncio
import logging
import logging.handlers

import discord
import uvicorn
from discord.ext import commands

from shared.config import (
    DISCORD_BERRIES_CHANNEL_WHITELIST_IDS,
    DISCORD_BOT_WEBHOOK_PORT,
    DISCORD_TOKEN,
    DISCORD_WATCH_CHANNEL_IDS,
    LOGS_DIR,
)
from shared.movie_db import init_movie_db
from shared.user_db import init_db as init_user_db
from discord_bot.webhook import create_webhook_app

# ── Logging ────────────────────────────────────────────────────────────────

def _setup_logger() -> logging.Logger:
    logger = logging.getLogger("discord_bot")
    logger.setLevel(logging.DEBUG)

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Console handler — INFO and above
    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)
    logger.addHandler(ch)

    # File handler — DEBUG and above, rotates at 5 MB, keeps 3 backups
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    fh = logging.handlers.RotatingFileHandler(
        LOGS_DIR / "discord_bot.log",
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    return logger


log = _setup_logger()

# ── Bot setup ──────────────────────────────────────────────────────────────

_EXTENSIONS = (
    "discord_bot.cogs.mention",
    "discord_bot.cogs.watcher",
    "discord_bot.cogs.moderation",
    "discord_bot.cogs.movies",
    "discord_bot.cogs.profile",
)

intents = discord.Intents.default()
intents.message_content = True


class BerriesBot(commands.Bot):
    async def setup_hook(self) -> None:
        for ext in _EXTENSIONS:
            await self.load_extension(ext)
            log.info("Loaded extension %s", ext)


bot = BerriesBot(command_prefix="!", intents=intents)


@bot.event
async def on_ready() -> None:
    log.info("Logged in as %s (id: %s)", bot.user, bot.user.id)
    log.info("Berries channel whitelist IDs: %s", DISCORD_BERRIES_CHANNEL_WHITELIST_IDS)
    log.info("Watch channel IDs: %s", DISCORD_WATCH_CHANNEL_IDS)
    try:
        init_user_db()
        init_movie_db()
    except Exception:
        log.exception("Failed to initialize databases")
    try:
        synced = await bot.tree.sync()
        log.info("Synced %d slash command(s)", len(synced))
    except Exception:
        log.exception("Failed to sync slash commands")


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: Exception) -> None:
    log.exception("Slash command error in /%s: %s", interaction.command and interaction.command.qualified_name, error)
    msg = "Something went wrong. Check the logs."
    try:
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
    except Exception:
        pass


# ── Webhook server (receives going-live events from ingest_api) ────────────

webhook_app = create_webhook_app(bot)


# ── Entry point ────────────────────────────────────────────────────────────

async def _main() -> None:
    if not DISCORD_TOKEN:
        raise RuntimeError("DISCORD_TOKEN is not set. Check your .env file.")

    # Bind to 127.0.0.1 only — ingest_api runs on the same host and is the sole
    # caller, so there's no reason to expose this port to the LAN.
    server = uvicorn.Server(
        uvicorn.Config(webhook_app, host="127.0.0.1", port=DISCORD_BOT_WEBHOOK_PORT, log_level="warning")
    )
    async with bot:
        await asyncio.gather(bot.start(DISCORD_TOKEN), server.serve())


if __name__ == "__main__":
    asyncio.run(_main())
