"""
discord_bot/main.py

Discord bot for Berries' community server.

Responsibilities:
  - Respond to @mentions anywhere in the server (RAG-backed)
  - Slash commands: /ping, /movie suggest add|remove|list, /movie announce, /movie history list|remove
  - Webhook server (port 8002) for going-live events forwarded from ingest_api

Run with:
    python -m discord_bot.main
"""

import asyncio
import logging
import logging.handlers
import random
import uuid
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import discord
import httpx
import uvicorn
from discord import app_commands
from discord.ext import commands
from fastapi import FastAPI, Request

from shared.chroma_client import get_collection
from shared.config import (
    CHUNK_TOKEN_LIMIT,
    CHROMA_N_RESULTS,
    DISCORD_ANNOUNCE_CHANNEL_ID,
    DISCORD_BERRIES_CHANNEL_WHITELIST_IDS,
    DISCORD_BERRIES_CHAT_CHANNEL_ID,
    DISCORD_BOT_WEBHOOK_PORT,
    DISCORD_CHUNK_OVERLAP_MESSAGES,
    DISCORD_EVENT_ROLE_ID,
    DISCORD_LOG_CHANNEL_ID,
    DISCORD_RULES_STICKER_ID,
    DISCORD_STICKERS_ONLY_CHANNEL_IDS,
    DISCORD_STREAM_ROLE_ID,
    DISCORD_TOKEN,
    DISCORD_WATCH_CHANNEL_IDS,
    GIPHY_API_KEY,
    LOGS_DIR,
    OMDB_API_KEY,
    PERSONALITY_FILE,
    TWITCH_CHANNEL,
)
from shared.tokenizer import count_tokens
from shared.llm_client import get_completion
from shared.prompt_builder import build_system_prompt, ContextType
from shared.movie_db import (
    add_suggestion,
    get_all_suggestions,
    get_all_watched,
    get_recent_watched,
    get_suggestion,
    init_movie_db,
    mark_watched,
    remove_suggestion,
    remove_watched,
)
from shared.user_db import init_db as init_user_db, link_discord, get_twitch_link, get_discord_for_twitch, set_nickname, get_user

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

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)

# ── Webhook server (receives going-live events from ingest_api) ────────────

webhook_app = FastAPI(title="Berries Discord Webhook")


# ── Stickers-only enforcement ──────────────────────────────────────────────
# Cached GuildSticker object so we don't fetch it on every violation.
_rules_sticker_cache: discord.GuildSticker | None = None


async def _get_rules_sticker(guild: discord.Guild) -> discord.GuildSticker | None:
    global _rules_sticker_cache
    if _rules_sticker_cache is not None:
        return _rules_sticker_cache
    if not DISCORD_RULES_STICKER_ID:
        return None
    try:
        _rules_sticker_cache = await guild.fetch_sticker(DISCORD_RULES_STICKER_ID)
    except Exception:
        log.exception("Failed to fetch rules sticker %s", DISCORD_RULES_STICKER_ID)
    return _rules_sticker_cache


# ── Watch channel buffer ────────────────────────────────────────────────────
# Each channel gets its own list of {"source", "text", "timestamp"} entries.
# Flushed to ChromaDB at CHUNK_TOKEN_LIMIT tokens or CHUNK_TIMEOUT_SEC inactivity.

_watch_buffers: dict[int, list[dict]] = {}


async def _flush_watch_channel(channel_id: int, reason: str) -> None:
    buf = _watch_buffers.get(channel_id)
    if not buf:
        return

    channel = bot.get_channel(channel_id)
    channel_name = getattr(channel, "name", str(channel_id))
    guild_id = str(channel.guild.id) if channel and hasattr(channel, "guild") and channel.guild else ""

    now = datetime.now(timezone.utc)
    chunk_id = f"discord_{now.strftime('%Y-%m-%dT%H-%M-%S')}_{uuid.uuid4().hex[:6]}"
    start_ts = datetime.fromtimestamp(buf[0]["timestamp"], tz=timezone.utc).isoformat()
    end_ts = datetime.fromtimestamp(buf[-1]["timestamp"], tz=timezone.utc).isoformat()
    text = "\n".join(e["text"] for e in buf)
    token_count = count_tokens(text)

    try:
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
    _watch_buffers[channel_id] = overlap



# ── Helpers ────────────────────────────────────────────────────────────────

def _load_personality() -> str:
    if PERSONALITY_FILE.exists():
        return PERSONALITY_FILE.read_text(encoding="utf-8").strip()
    return "You are Berries, a playful forest demon."

def _get_chroma_context(query: str) -> str:
    try:
        collection = get_collection()
        results = collection.query(query_texts=[query], n_results=CHROMA_N_RESULTS)
        docs = results.get("documents", [[]])[0]
        log.debug("ChromaDB returned %d doc(s) for query: %.80r", len(docs), query)
        if docs:
            return (
                "RELEVANT PAST CONTEXT:\n"
                "The following excerpts from past stream logs may be relevant to the conversation. "
                "Use them to inform your response if helpful — do not quote them directly.\n"
                + "\n---\n".join(docs)
            )
    except Exception:
        log.exception("ChromaDB query failed")
    return ""


async def _get_chroma_context_with_assistant(
    query: str,
    recent_context: str = "",
    username: str = "",
) -> tuple[str, list[str] | None]:
    """
    Returns (context_block, search_queries).
    search_queries is None if the rewriter returned SKIP, or a list of strings otherwise.
    """
    try:
        from shared.llm_client import rewrite_queries
        from shared.chroma_client import query_chroma_multi

        search_queries = await rewrite_queries(query, recent_context, username or "a Discord user")
        if search_queries is None:  # SKIP — no retrieval needed
            log.debug("rewrite_queries returned SKIP for query: %.80r", query)
            return "", None

        docs = query_chroma_multi(search_queries)
        log.debug(
            "ChromaDB returned %d unique doc(s) for %d rewritten quer(ies): %.80r",
            len(docs), len(search_queries), query,
        )
        if docs:
            return (
                "RELEVANT PAST CONTEXT:\n"
                "The following excerpts from past stream logs may be relevant to the conversation. "
                "Use them to inform your response if helpful — do not quote them directly.\n"
                + "\n---\n".join(docs)
            ), search_queries
        return "", search_queries
    except Exception:
        log.exception("ChromaDB query failed")
    return "", []


async def _get_channel_history(channel: discord.TextChannel, before: discord.Message, limit: int = 20) -> str:
    """Fetch the last `limit` messages from `channel` before `message` and format them."""
    try:
        messages = [m async for m in channel.history(limit=limit, before=before)]
        messages.reverse()
        lines = [
            f"{m.author.display_name}: {m.content}"
            for m in messages
            if m.content
        ]
        if not lines:
            return ""
        return (
            "=== RECENT CHANNEL MESSAGES ===\n"
            "Here are the most recent messages in the channel, which may help you provide context and continuity to your response:\n"
            + "\n".join(lines)
        )
    except Exception:
        log.exception("Failed to fetch channel history for channel %s", channel.id)
        return ""

def cleanup_response(text: str) -> str:
    """Clean up LLM response by stripping out any instances of italicized roleplay and double line breaks."""
    import re
    # Remove lines that are entirely italicized roleplay actions (e.g. "*does a thing*")
    text = re.sub(r"^\*[^*\n]+\*\s*$", "", text, flags=re.MULTILINE)
    text = re.sub(r"\n{2,}", "\n", text)  # Replace 2 or more line breaks with just 1
    return text.strip()

async def _llm(
    user_message: str,
    context_type: ContextType = ContextType.DISCORD_MENTION,
    context: str = "",
) -> str:
    """Call the LLM with Berries' personality and context-appropriate instructions."""
    personality = _load_personality()
    system = build_system_prompt(personality, context_type, context)
    log.debug("LLM call — user_message: %.120r", user_message)
    response = await get_completion(system_prompt=system, user_message=user_message, max_tokens=600)
    log.debug("LLM response: %.120r", response)
    return response


async def _omdb_search(title: str) -> dict | None:
    """Search OMDb for a movie by title. Returns the first result dict or None."""
    results = await _omdb_search_many(title, limit=1)
    return results[0] if results else None


async def _omdb_search_many(title: str, limit: int = 5) -> list[dict]:
    """Search OMDb for a movie by title. Returns up to `limit` result dicts."""
    if not OMDB_API_KEY:
        log.debug("OMDB_API_KEY not set; skipping OMDb search")
        return []
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                "https://www.omdbapi.com/",
                params={"s": title, "type": "movie", "apikey": OMDB_API_KEY},
                timeout=5.0,
            )
        data = resp.json()
        if data.get("Response") == "True" and data.get("Search"):
            results = data["Search"][:limit]
            log.debug("OMDb found %d result(s) for %r (returning %d)", len(data["Search"]), title, len(results))
            return results
        log.debug("OMDb returned no results for %r: %s", title, data.get("Error", "unknown"))
    except Exception:
        log.exception("OMDb search failed for %r", title)
    return []


async def _gif_search_query(context: str) -> str:
    """Ask Berries to pick a Tenor search query that fits the announcement context."""
    return await get_completion(
        system_prompt="You generate short Tenor GIF search queries. Reply with ONLY the search query, 2-5 words, no punctuation, no explanation.",
        user_message=context,
    )


async def _fetch_gif(query: str) -> str | None:
    """Search Giphy and return a random GIF URL from the top results."""
    log.debug("Fetching GIF for query: %.80r", query)
    if not GIPHY_API_KEY:
        log.debug("GIPHY_API_KEY not set; skipping GIF search")
        return None
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                "https://api.giphy.com/v1/gifs/search",
                params={"q": query, "api_key": GIPHY_API_KEY, "limit": 8, "rating": "pg-13"},
                timeout=5.0,
            )
        results = resp.json().get("data", [])
        if not results:
            log.debug("Giphy returned no results for %r", query)
            return None
        pick = random.choice(results[:5])
        return pick.get("images", {}).get("original", {}).get("url")
    except Exception:
        log.exception("Giphy search failed for %r", query)
        return None


async def _post_to_announce(message: str) -> bool:
    """Post a message to the announce channel. Returns True on success."""
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


async def _count_recent_bot_messages(channel: discord.TextChannel, before: discord.Message, limit: int = 20) -> int:
    """Count how many of the last `limit` messages were sent by the bot."""
    try:
        messages = [m async for m in channel.history(limit=limit, before=before)]
        return sum(1 for m in messages if m.author == bot.user)
    except Exception:
        log.exception("Failed to count bot messages in channel %s", channel.id)
        return 0


# ── Events ─────────────────────────────────────────────────────────────────

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


@bot.event
async def on_message(message: discord.Message) -> None:
    # Buffer watch channel messages — runs for all messages including bots and Berries herself
    if DISCORD_WATCH_CHANNEL_IDS and message.channel.id in DISCORD_WATCH_CHANNEL_IDS and message.content:
        channel_id = message.channel.id
        if channel_id not in _watch_buffers:
            _watch_buffers[channel_id] = []
        _watch_buffers[channel_id].append({
            "source": message.author.display_name,
            "text": f"[{message.author.display_name}]: {message.content}",
            "timestamp": message.created_at.timestamp(),
        })
        log.debug("Watch buffer #%s: %d entries", channel_id, len(_watch_buffers[channel_id]))
        buf_text = "\n".join(e["text"] for e in _watch_buffers[channel_id])
        if count_tokens(buf_text) >= CHUNK_TOKEN_LIMIT:
            await _flush_watch_channel(channel_id, reason="token_limit")

    if message.author == bot.user:
        return

    # ── Stickers-only channel enforcement ──────────────────────────────────
    if DISCORD_STICKERS_ONLY_CHANNEL_IDS and message.channel.id in DISCORD_STICKERS_ONLY_CHANNEL_IDS:
        member = message.author if isinstance(message.author, discord.Member) else None
        is_mod = member is not None and member.guild_permissions.manage_messages
        if not is_mod and not message.stickers:
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
            sticker = await _get_rules_sticker(message.guild)
            if sticker:
                try:
                    await message.channel.send(stickers=[sticker])
                except Exception:
                    log.exception("Failed to send rules sticker in channel %s", message.channel.id)
        return

    mentioned = bot.user in message.mentions and not message.mention_everyone

    if not mentioned:
        await bot.process_commands(message)
        return

    content = message.content.replace(f"<@{bot.user.id}>", "").replace(f"<@!{bot.user.id}>", "").strip()

    if not content:
        log.debug("Ignoring empty message from %s in channel %s", message.author, message.channel.id)
        return

    log.info(
        "Responding to %s in channel %s (mentioned=%s): %.120r",
        message.author, message.channel.id, mentioned, content,
    )

    # If Berries was @mentioned in a non-whitelisted channel and has already spoken
    # twice in the recent history, redirect to #berries-chat instead of responding.
    if (
        DISCORD_BERRIES_CHAT_CHANNEL_ID
        and message.channel.id not in DISCORD_BERRIES_CHANNEL_WHITELIST_IDS
    ):
        bot_count = await _count_recent_bot_messages(message.channel, before=message)
        if bot_count >= 2:
            log.info(
                "Redirecting %s to berries-chat (%d recent bot messages in channel %s)",
                message.author, bot_count, message.channel.id,
            )
            await message.channel.send(
                f"Hey, there's a lot of people here and it's making me anxious to talk here too much. "
                f"If you want to have a conversation, let's talk in <#{DISCORD_BERRIES_CHAT_CHANNEL_ID}>"
            )
            await bot.process_commands(message)
            return

    try:
        async with message.channel.typing():
            user_display_name = message.author.display_name
            history = await _get_channel_history(message.channel, before=message)
            context, search_queries = await _get_chroma_context_with_assistant(content, recent_context=history, username=user_display_name)
            system_suffix = "\n\n".join(filter(None, [context, history]))
            log.debug("Calling LLM for on_message")
            t_login = get_twitch_link(str(message.author.id))
            _db_user = get_user(t_login) if t_login else None
            user_nickname = (_db_user.get("nickname") or user_display_name) if _db_user else user_display_name
            user_nickname_str = f" (nickname: {user_nickname})" if user_nickname != user_display_name else ""
            date_time_str = message.created_at.replace(tzinfo=timezone.utc).astimezone(ZoneInfo("America/Chicago")).strftime("%A, %Y-%m-%d %H:%M:%S")
            user_msg = f"(datetime: {date_time_str} [US Central Time]) {user_display_name}{user_nickname_str} said: {content}"
            response = await _llm(user_msg, context=system_suffix)
            log.debug("LLM response for on_message: %.120r", response)

            from shared.call_logger import log_llm_call
            log_llm_call(
                service="discord",
                username=user_display_name,
                raw_message=content,
                rewrite_queries=search_queries,
                system_prompt=build_system_prompt(_load_personality(), ContextType.DISCORD_MENTION, system_suffix),
                user_message=user_msg,
                response=response,
            )

        await message.channel.send(response)
        log.info("Sent response to %s in channel %s", message.author, message.channel.id)
    except Exception:
        log.exception(
            "Failed to generate/send response to %s in channel %s",
            message.author, message.channel.id,
        )

    await bot.process_commands(message)


# ── Movie disambiguation UI ────────────────────────────────────────────────

class MovieSelectView(discord.ui.View):
    """Ephemeral dropdown for /movie suggest add disambiguation."""

    def __init__(self, results: list[dict], invoker: discord.User | discord.Member) -> None:
        super().__init__(timeout=60)
        self.selected: dict | None = None
        self.cancelled: bool = False
        self._results = results
        self._invoker = invoker

        options = [
            discord.SelectOption(label=f"{m['Title']} ({m['Year']})"[:100], value=str(i))
            for i, m in enumerate(results)
        ]
        options.append(discord.SelectOption(label="Cancel", value="cancel"))

        select = discord.ui.Select(placeholder="Pick a movie...", options=options)
        select.callback = self._callback
        self.add_item(select)

    async def _callback(self, interaction: discord.Interaction) -> None:
        if interaction.user != self._invoker:
            await interaction.response.send_message("This isn't your selection.", ephemeral=True)
            return
        value = interaction.data["values"][0]
        if value == "cancel":
            self.cancelled = True
        else:
            self.selected = self._results[int(value)]
        await interaction.response.defer()
        self.stop()


# ── Slash commands ─────────────────────────────────────────────────────────

@bot.tree.command(name="ping", description="Check if Berries is lurking")
async def ping(interaction: discord.Interaction) -> None:
    await interaction.response.send_message("*stares from the shadows* ...yes, I am here. :3")


@bot.tree.command(name="twitch-link", description="Link your Twitch account to your Discord profile")
@app_commands.describe(twitch_username="Your Twitch username (without the @)")
async def twitch_link(interaction: discord.Interaction, twitch_username: str) -> None:
    await interaction.response.defer(ephemeral=True)

    import re
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

    if DISCORD_LOG_CHANNEL_ID and status != "already_linked":
        try:
            log_channel = bot.get_channel(DISCORD_LOG_CHANNEL_ID) or await bot.fetch_channel(DISCORD_LOG_CHANNEL_ID)
            detail = f" (was `{previous}`)" if previous else ""
            await log_channel.send(
                f"**Twitch link** | {interaction.user.mention} (`{interaction.user}`) "
                f"→ `{twitch_username}` | status: `{status}`{detail}"
            )
        except Exception as e:
            log.warning("Failed to send to log channel %s: %s", DISCORD_LOG_CHANNEL_ID, e)


@bot.tree.command(name="set-nickname", description="Set the nickname Berries uses for you")
@app_commands.describe(nickname="What you'd like Berries to call you (max 32 characters)")
async def set_nickname_cmd(interaction: discord.Interaction, nickname: str) -> None:
    await interaction.response.defer(ephemeral=True)

    discord_id = str(interaction.user.id)
    t_login = get_twitch_link(discord_id)

    if not t_login:
        await interaction.followup.send(
            "*tilts head* ...I don't recognise you yet. "
            "Link your Twitch account first with `/twitch-link`, then come back.",
            ephemeral=True,
        )
        return

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

    set_nickname(t_login, nickname)

    log.info(
        "Nickname set: Discord user %s (%s) / Twitch %r → %r",
        interaction.user, discord_id, t_login, nickname,
    )

    await interaction.followup.send(
        f"*rustles quietly* ...understood. I'll call you **{nickname}** from now on.",
        ephemeral=True,
    )

    if DISCORD_LOG_CHANNEL_ID:
        try:
            log_channel = bot.get_channel(DISCORD_LOG_CHANNEL_ID) or await bot.fetch_channel(DISCORD_LOG_CHANNEL_ID)
            await log_channel.send(
                f"**Nickname set** | {interaction.user.mention} (`{interaction.user}`) "
                f"/ Twitch `{t_login}` → `{nickname}`"
            )
        except Exception as e:
            log.warning("Failed to send to log channel %s: %s", DISCORD_LOG_CHANNEL_ID, e)


# ── /movie subcommand group ─────────────────────────────────────────────────

movie_group = app_commands.Group(name="movie", description="Movie night commands")
suggest_group = app_commands.Group(name="suggest", description="Manage movie suggestions", parent=movie_group)
history_group = app_commands.Group(name="history", description="View and manage movie watch history", parent=movie_group)


@suggest_group.command(name="add", description="Suggest a movie for movie night")
@app_commands.describe(title="Movie title to search for")
async def movie_suggest_add(interaction: discord.Interaction, title: str) -> None:
    await interaction.response.defer(ephemeral=True)

    results = await _omdb_search_many(title, limit=5)
    if not results:
        await interaction.followup.send(
            f"*rustles in the shadows* ...I couldn't find **{title}** on OMDb. Try a more specific title?",
            ephemeral=True,
        )
        return

    # If multiple results, show an ephemeral dropdown — only the invoking user sees it
    if len(results) > 1:
        lines = [f"*Found a few matches for **{title}**. Which one did you mean?*\n"]
        for i, m in enumerate(results, 1):
            lines.append(f"{i}. **{m['Title']}** ({m['Year']})")

        view = MovieSelectView(results, interaction.user)
        await interaction.followup.send("\n".join(lines), view=view, ephemeral=True)
        await view.wait()

        if view.cancelled:
            await interaction.followup.send(
                "*retreats into the forest* ...alright, cancelled.", ephemeral=True
            )
            return
        if view.selected is None:
            await interaction.followup.send(
                "*the shadows grow quiet* ...selection timed out. Run `/movie suggest add` again if you'd like to try.",
                ephemeral=True,
            )
            return

        result = view.selected
    else:
        result = results[0]

    imdb_id = result["imdbID"]
    movie_title = result["Title"]
    year = result["Year"]

    existing = get_suggestion(imdb_id)
    if existing and existing["status"] == "suggested":
        await interaction.followup.send(
            f"**{movie_title} ({year})** is already on the suggestion list!", ephemeral=True
        )
        return

    recent = get_recent_watched(365)
    recently_watched = next((m for m in recent if m["imdb_id"] == imdb_id), None)
    if recently_watched:
        watched_date = recently_watched["watched_at"][:10]
        rejection = await _llm(
            f"Someone just suggested '{movie_title} ({year})' for movie night, but we already watched it "
            f"on {watched_date} (less than a year ago). Reject the suggestion in-character — be playful. Keep it short."
        )
        await interaction.followup.send(rejection, ephemeral=True)
        return

    add_suggestion(imdb_id, movie_title, year, interaction.user.display_name)
    log.info("Added suggestion %r (%s) by %s", movie_title, imdb_id, interaction.user)
    # Post public confirmation so the channel knows what was added
    await interaction.channel.send(
        f"**{interaction.user.display_name}** added **{movie_title} ({year})** to the movie night suggestion list!"
    )


@suggest_group.command(name="list", description="See the current movie night suggestion list")
async def movie_suggest_list(interaction: discord.Interaction) -> None:
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


@suggest_group.command(name="remove", description="Remove a movie from the suggestion list")
@app_commands.default_permissions(manage_messages=True)
@app_commands.describe(title="Title of the movie to remove")
async def movie_suggest_remove(interaction: discord.Interaction, title: str) -> None:
    await interaction.response.defer()

    result = await _omdb_search(title)
    if not result:
        await interaction.followup.send(
            f"*rustles in the shadows* ...couldn't find **{title}** on OMDb. Try a more specific title?"
        )
        return

    imdb_id = result["imdbID"]
    movie_title = result["Title"]
    year = result["Year"]

    removed = remove_suggestion(imdb_id)
    if removed:
        log.info("Removed suggestion %r (%s) by %s", movie_title, imdb_id, interaction.user)
        await interaction.followup.send(f"Removed **{movie_title} ({year})** from the suggestion list.")
    else:
        await interaction.followup.send(
            f"**{movie_title} ({year})** isn't on the suggestion list."
        )


@movie_group.command(name="announce", description="Announce tonight's movie and mark it as watched")
@app_commands.default_permissions(manage_messages=True)
@app_commands.describe(title="Movie title to announce", notes="Optional notes from Twig about this movie")
async def movie_announce(interaction: discord.Interaction, title: str, notes: str = "") -> None:
    await interaction.response.defer()

    result = await _omdb_search(title)
    if not result:
        await interaction.followup.send(f"Couldn't find **{title}** on OMDb.")
        return

    imdb_id = result["imdbID"]
    movie_title = result["Title"]
    year = result["Year"]

    prompt = (
        f"Tonight's movie night movie is: {movie_title} ({year}). "
        f"Write a short in-character announcement for the Discord server. "
        f"Give your genuine reaction to this movie choice and hype people up to join either in VRChat or in the Discord stream. "
        f"2-3 sentences, stay in character."
    )
    if notes:
        prompt += f" Additional notes about this movie from Twig: {notes}"

    announcement = await _llm(prompt, context_type=ContextType.DISCORD_ANNOUNCE)

    # Ensure the movie exists in the DB before marking watched
    if not get_suggestion(imdb_id):
        add_suggestion(imdb_id, movie_title, year, interaction.user.display_name)
    mark_watched(imdb_id)

    gif_query = await _gif_search_query(f"Pick a GIF search term for a movie night announcement about: {movie_title} ({year})")
    gif_url = await _fetch_gif(gif_query.strip())
    role_ping = f"<@&{DISCORD_EVENT_ROLE_ID}>\n" if DISCORD_EVENT_ROLE_ID else ""
    message = f"# {movie_title} ({year})\n" + role_ping + announcement

    posted = await _post_to_announce(message)
    if posted:
        if gif_url:
            channel = bot.get_channel(DISCORD_ANNOUNCE_CHANNEL_ID)
            if channel:
                await channel.send(gif_url)
        await interaction.followup.send(
            f"Announced **{movie_title}** in <#{DISCORD_ANNOUNCE_CHANNEL_ID}> and marked as watched!"
        )
    else:
        await interaction.followup.send(message + (f"\n{gif_url}" if gif_url else ""))


@history_group.command(name="list", description="See movies we've already watched")
async def movie_history_list(interaction: discord.Interaction) -> None:
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


@history_group.command(name="remove", description="Remove a movie from the watch history")
@app_commands.default_permissions(manage_messages=True)
@app_commands.describe(title="Title of the movie to remove from history")
async def movie_history_remove(interaction: discord.Interaction, title: str) -> None:
    await interaction.response.defer()

    result = await _omdb_search(title)
    if not result:
        await interaction.followup.send(
            f"*rustles in the shadows* ...couldn't find **{title}** on OMDb. Try a more specific title?"
        )
        return

    imdb_id = result["imdbID"]
    movie_title = result["Title"]
    year = result["Year"]

    removed = remove_watched(imdb_id)
    if removed:
        log.info("Removed %r (%s) from watch history by %s", movie_title, imdb_id, interaction.user)
        await interaction.followup.send(f"Removed **{movie_title} ({year})** from the watch history.")
    else:
        await interaction.followup.send(
            f"**{movie_title} ({year})** isn't in the watch history."
        )


bot.tree.add_command(movie_group)


# ── Webhook handlers ───────────────────────────────────────────────────────

@webhook_app.post("/event/going-live")
async def going_live(request: Request) -> dict:
    """Called by ingest_api when Streamer.bot fires a going-live event."""
    body = await request.json()
    stream_title = body.get("title", "")
    category = body.get("category", "")
    log.info("Going-live event received: title=%r, category=%r", stream_title, category)

    announcement = await _llm(
        f"TwigOtter just went live on Twitch! Stream title: '{stream_title}', category: '{category}'. "
        f"Write a short in-character going-live announcement for the Discord server that tells readers what to expect based on the title and category. "
        f"Get people hyped to come watch. 2-3 sentences, stay in character.",
        context_type=ContextType.DISCORD_ANNOUNCE,
    )

    gif_query = await _gif_search_query(f"Pick a GIF search term for a Twitch going-live announcement. Stream: '{stream_title}', category: '{category}'")
    gif_url = await _fetch_gif(gif_query.strip())
    role_ping = f"<@&{DISCORD_STREAM_ROLE_ID}>\n" if DISCORD_STREAM_ROLE_ID else ""
    message = role_ping + announcement + f"\nhttps://twitch.tv/{TWITCH_CHANNEL}"

    await _post_to_announce(message)
    if gif_url:
        channel = bot.get_channel(DISCORD_ANNOUNCE_CHANNEL_ID)
        if channel:
            await channel.send(gif_url)
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
