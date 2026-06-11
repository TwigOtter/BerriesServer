"""
discord_bot/webhook.py

FastAPI app that receives going-live events forwarded from ingest_api.
Bound to 127.0.0.1 only (see main.py) — not reachable over the LAN.
Authenticated via the shared INGEST_SECRET header, same as ingest_api.
"""

import hmac
import logging

import discord
from fastapi import FastAPI, Header, HTTPException, Request

from shared.ask_berries import ask_berries_twitch_going_live
from shared.config import (
    DISCORD_ANNOUNCE_CHANNEL_ID,
    DISCORD_STREAM_ROLE_ID,
    INGEST_SECRET,
    TWITCH_CHANNEL,
)
from discord_bot.services import fetch_gif

log = logging.getLogger("discord_bot.webhook")


def _webhook_auth(x_secret: str | None) -> None:
    """Reject webhook requests that don't carry the shared INGEST_SECRET.

    Uses constant-time comparison. Requires INGEST_SECRET to be set — refuses
    all requests otherwise so a misconfigured deploy fails closed.
    """
    if not INGEST_SECRET:
        raise HTTPException(status_code=503, detail="Webhook auth not configured")
    if not hmac.compare_digest(x_secret or "", INGEST_SECRET):
        raise HTTPException(status_code=403, detail="Forbidden")


async def post_to_announce(bot: discord.Client, message: str) -> bool:
    """Post a message to the announce channel. Returns True on success."""
    if not message.strip():
        log.debug("Not posting empty message to announce channel")
        return False
    if not DISCORD_ANNOUNCE_CHANNEL_ID:
        log.warning("DISCORD_ANNOUNCE_CHANNEL_ID not set; cannot post announcement")
        return False
    channel = bot.get_channel(DISCORD_ANNOUNCE_CHANNEL_ID)
    if not channel:
        log.error("Announce channel %s not found in cache", DISCORD_ANNOUNCE_CHANNEL_ID)
        return False
    try:
        await channel.send(message)
        log.info("Posted announcement to channel %s", DISCORD_ANNOUNCE_CHANNEL_ID)
        return True
    except Exception:
        log.exception("Failed to post to announce channel %s", DISCORD_ANNOUNCE_CHANNEL_ID)
        return False


def create_webhook_app(bot: discord.Client) -> FastAPI:
    app = FastAPI(title="Berries Discord Webhook")

    @app.post("/event/going-live")
    async def going_live(
        request: Request,
        x_secret: str | None = Header(default=None),
    ) -> dict:
        """Called by ingest_api when Streamer.bot fires a going-live event."""
        _webhook_auth(x_secret)
        body = await request.json()
        stream_title = body.get("title", "")
        category = body.get("category", "")
        log.info("Going-live event received: title=%r, category=%r", stream_title, category)

        result_pair = await ask_berries_twitch_going_live(stream_title, category)
        if not result_pair:
            log.warning("ask_berries_twitch_going_live returned None — skipping announcement")
            return {"status": "error", "reason": "announcement generation failed"}
        announcement, gif_query = result_pair

        gif_url = await fetch_gif(gif_query) if gif_query else None
        role_ping = f"<@&{DISCORD_STREAM_ROLE_ID}>\n" if DISCORD_STREAM_ROLE_ID else ""
        message = role_ping + announcement + f"\nhttps://twitch.tv/{TWITCH_CHANNEL}"

        await post_to_announce(bot, message)
        if gif_url:
            await post_to_announce(bot, gif_url)
        return {"status": "ok"}

    return app
