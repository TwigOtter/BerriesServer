"""
discord_bot/main.py

Discord bot for Berries' community server.

Responsibilities:
  - Respond to @mentions anywhere in the server (RAG-backed)
  - Respond to regular messages in whitelisted channels (RAG-backed)
  - Slash commands: /ping, /suggest-movie, /suggested-movies, /past-movies, /movie-time
  - Webhook server (port 8002) for going-live events forwarded from ingest_api

Run with:
    python -m discord_bot.main
"""

import asyncio
import random

import discord
import httpx
import uvicorn
from discord import app_commands
from discord.ext import commands
from fastapi import FastAPI, Request

from shared.chroma_client import get_collection
from shared.config import (
    CHROMA_N_RESULTS,
    DISCORD_ANNOUNCE_CHANNEL_ID,
    DISCORD_BERRIES_CHANNEL_IDS,
    DISCORD_BOT_WEBHOOK_PORT,
    DISCORD_TOKEN,
    GIPHY_API_KEY,
    OMDB_API_KEY,
    PERSONALITY_FILE,
)
from shared.llm_client import get_completion
from shared.movie_db import (
    add_suggestion,
    get_all_suggestions,
    get_all_watched,
    get_recent_watched,
    get_suggestion,
    init_movie_db,
    mark_watched,
)

# ── Bot setup ──────────────────────────────────────────────────────────────

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)

# ── Webhook server (receives going-live events from ingest_api) ────────────

webhook_app = FastAPI(title="Berries Discord Webhook")


# ── Helpers ────────────────────────────────────────────────────────────────

def _load_personality() -> str:
    if PERSONALITY_FILE.exists():
        return PERSONALITY_FILE.read_text(encoding="utf-8").strip()
    return "You are Berries, a playful forest demon."


def _get_chroma_context(query: str) -> str:
    collection = get_collection()
    results = collection.query(query_texts=[query], n_results=CHROMA_N_RESULTS)
    docs = results.get("documents", [[]])[0]
    if docs:
        return "=== RELEVANT PAST STREAM CONTEXT ===\n" + "\n\n".join(docs)
    return ""


async def _llm(user_message: str, system_suffix: str = "") -> str:
    """Call the LLM with Berries' personality plus an optional extra system block."""
    personality = _load_personality()
    system = personality + (f"\n\n{system_suffix}" if system_suffix else "")
    return await get_completion(system_prompt=system, user_message=user_message)


async def _omdb_search(title: str) -> dict | None:
    """Search OMDb for a movie by title. Returns the first result dict or None."""
    if not OMDB_API_KEY:
        return None
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            "https://www.omdbapi.com/",
            params={"s": title, "type": "movie", "apikey": OMDB_API_KEY},
            timeout=5.0,
        )
    data = resp.json()
    if data.get("Response") == "True" and data.get("Search"):
        return data["Search"][0]  # {"Title", "Year", "imdbID", "Type", "Poster"}
    return None


async def _gif_search_query(context: str) -> str:
    """Ask Berries to pick a Tenor search query that fits the announcement context."""
    return await get_completion(
        system_prompt="You generate short Tenor GIF search queries. Reply with ONLY the search query, 2-5 words, no punctuation, no explanation.",
        user_message=context,
    )


async def _fetch_gif(query: str) -> str | None:
    """Search Giphy and return a random GIF URL from the top results."""
    if not GIPHY_API_KEY:
        return None
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            "https://api.giphy.com/v1/gifs/search",
            params={"q": query, "api_key": GIPHY_API_KEY, "limit": 8, "rating": "pg-13"},
            timeout=5.0,
        )
    results = resp.json().get("data", [])
    if not results:
        return None
    pick = random.choice(results[:5])
    return pick.get("images", {}).get("original", {}).get("url")


async def _post_to_announce(message: str) -> bool:
    """Post a message to the announce channel. Returns True on success."""
    if not DISCORD_ANNOUNCE_CHANNEL_ID:
        return False
    channel = bot.get_channel(DISCORD_ANNOUNCE_CHANNEL_ID)
    if not channel:
        print(f"[discord_bot] Announce channel {DISCORD_ANNOUNCE_CHANNEL_ID} not found in cache")
        return False
    await channel.send(message)
    return True


# ── Events ─────────────────────────────────────────────────────────────────

@bot.event
async def on_ready() -> None:
    print(f"[discord_bot] Logged in as {bot.user} (id: {bot.user.id})")
    print(f"[discord_bot] Watching channel IDs: {DISCORD_BERRIES_CHANNEL_IDS}")
    init_movie_db()
    synced = await bot.tree.sync()
    print(f"[discord_bot] Synced {len(synced)} slash command(s)")


@bot.event
async def on_message(message: discord.Message) -> None:
    if message.author == bot.user:
        return

    mentioned = bot.user in message.mentions and not message.mention_everyone

    if not mentioned:
        # Outside whitelisted channels, ignore regular messages
        if DISCORD_BERRIES_CHANNEL_IDS and message.channel.id not in DISCORD_BERRIES_CHANNEL_IDS:
            await bot.process_commands(message)
            return

    content = message.content
    if mentioned:
        content = content.replace(f"<@{bot.user.id}>", "").replace(f"<@!{bot.user.id}>", "").strip()

    if not content:
        return

    async with message.channel.typing():
        context = _get_chroma_context(content)
        system_prompt = _load_personality() + (f"\n\n{context}" if context else "")
        user_message = f"{message.author.display_name}: {content}"
        response = await get_completion(system_prompt=system_prompt, user_message=user_message)

    await message.channel.send(response)
    await bot.process_commands(message)


# ── Slash commands ─────────────────────────────────────────────────────────

@bot.tree.command(name="ping", description="Check if Berries is lurking")
async def ping(interaction: discord.Interaction) -> None:
    await interaction.response.send_message("*stares from the shadows* ...yes, I am here. :3")


@bot.tree.command(name="suggest-movie", description="Suggest a movie for movie night")
@app_commands.describe(title="Movie title to search for")
async def suggest_movie(interaction: discord.Interaction, title: str) -> None:
    await interaction.response.defer()

    result = await _omdb_search(title)
    if not result:
        await interaction.followup.send(
            f"*rustles in the shadows* ...I couldn't find **{title}** on OMDb. Try a more specific title?"
        )
        return

    imdb_id = result["imdbID"]
    movie_title = result["Title"]
    year = result["Year"]

    existing = get_suggestion(imdb_id)
    if existing and existing["status"] == "suggested":
        await interaction.followup.send(f"**{movie_title} ({year})** is already on the suggestion list!")
        return

    recent = get_recent_watched(365)
    recently_watched = next((m for m in recent if m["imdb_id"] == imdb_id), None)
    if recently_watched:
        watched_date = recently_watched["watched_at"][:10]
        rejection = await _llm(
            f"Someone just suggested '{movie_title} ({year})' for movie night, but we already watched it "
            f"on {watched_date} (less than a year ago). Reject the suggestion in-character — be playful. Keep it short."
        )
        await interaction.followup.send(rejection)
        return

    add_suggestion(imdb_id, movie_title, year, interaction.user.display_name)
    await interaction.followup.send(f"Added **{movie_title} ({year})** to the movie night list!")


@bot.tree.command(name="suggested-movies", description="See the current movie night suggestion list")
async def suggested_movies(interaction: discord.Interaction) -> None:
    suggestions = get_all_suggestions()
    if not suggestions:
        await interaction.response.send_message(
            "*peers out from the forest* ...no movies have been suggested yet."
        )
        return

    lines = ["**Movie Night Suggestions**\n"]
    for i, m in enumerate(suggestions, 1):
        lines.append(f"{i}. **{m['title']}** ({m['year']}) — suggested by {m['suggested_by']}")
    await interaction.response.send_message("\n".join(lines))


@bot.tree.command(name="past-movies", description="See movies we've already watched")
async def past_movies(interaction: discord.Interaction) -> None:
    watched = get_all_watched()
    if not watched:
        await interaction.response.send_message(
            "*tilts head* ...we haven't watched anything yet. Time to fix that."
        )
        return

    lines = ["**Movies We've Watched**\n"]
    for m in watched[:20]:
        date = m["watched_at"][:10] if m["watched_at"] else "?"
        lines.append(f"• **{m['title']}** ({m['year']}) — {date}")
    if len(watched) > 20:
        lines.append(f"\n*...and {len(watched) - 20} more*")
    await interaction.response.send_message("\n".join(lines))


@bot.tree.command(name="movie-time", description="Announce tonight's movie and mark it as watched")
@app_commands.describe(title="Movie title to announce")
async def movie_time(interaction: discord.Interaction, title: str) -> None:
    await interaction.response.defer()

    result = await _omdb_search(title)
    if not result:
        await interaction.followup.send(f"Couldn't find **{title}** on OMDb.")
        return

    imdb_id = result["imdbID"]
    movie_title = result["Title"]
    year = result["Year"]

    announcement = await _llm(
        f"Tonight's movie night movie is: {movie_title} ({year}). "
        f"Write a short in-character announcement for the Discord server. "
        f"Give your genuine reaction to this movie choice and hype people up to join. "
        f"2-3 sentences, stay in character."
    )

    # Ensure the movie exists in the DB before marking watched
    if not get_suggestion(imdb_id):
        add_suggestion(imdb_id, movie_title, year, interaction.user.display_name)
    mark_watched(imdb_id)

    gif_query = await _gif_search_query(f"Pick a GIF search term for a movie night announcement about: {movie_title} ({year})")
    gif_url = await _fetch_gif(gif_query.strip())
    message = announcement + (f"\n{gif_url}" if gif_url else "")

    posted = await _post_to_announce(message)
    if posted:
        await interaction.followup.send(
            f"Announced **{movie_title}** in <#{DISCORD_ANNOUNCE_CHANNEL_ID}> and marked as watched!"
        )
    else:
        await interaction.followup.send(message)


# ── Webhook handlers ───────────────────────────────────────────────────────

@webhook_app.post("/event/going-live")
async def going_live(request: Request) -> dict:
    """Called by ingest_api when Streamer.bot fires a going-live event."""
    body = await request.json()
    stream_title = body.get("title", "")
    category = body.get("category", "")

    announcement = await _llm(
        f"TwigOtter just went live on Twitch! Stream title: '{stream_title}', category: '{category}'. "
        f"Write a short in-character going-live announcement for the Discord server. "
        f"Get people hyped to come watch. 2-3 sentences, stay in character."
    )

    gif_query = await _gif_search_query(f"Pick a GIF search term for a Twitch going-live announcement. Stream: '{stream_title}', category: '{category}'")
    gif_url = await _fetch_gif(gif_query.strip())
    message = announcement + (f"\n{gif_url}" if gif_url else "")

    await _post_to_announce(message)
    return {"status": "ok"}


# ── Entry point ────────────────────────────────────────────────────────────

async def _main() -> None:
    if not DISCORD_TOKEN:
        raise RuntimeError("DISCORD_TOKEN is not set. Check your .env file.")

    server = uvicorn.Server(
        uvicorn.Config(webhook_app, host="0.0.0.0", port=DISCORD_BOT_WEBHOOK_PORT, log_level="warning")
    )
    async with bot:
        await asyncio.gather(bot.start(DISCORD_TOKEN), server.serve())


if __name__ == "__main__":
    asyncio.run(_main())
