"""
discord_bot/main.py

Discord bot for Berries' community server.
Berries responds in designated channel(s) using the same personality and
ChromaDB context as the Twitch bot. Discord messages are NOT stored back
into ChromaDB or transcript files (read-only context source).

Run with:
    python -m discord_bot.main
"""

import discord
from discord.ext import commands

from shared.config import DISCORD_TOKEN, DISCORD_BERRIES_CHANNEL_IDS
from shared.llm_client import get_completion
from shared.chroma_client import get_collection
from shared.config import CHROMA_N_RESULTS, PERSONALITY_FILE

# ── Bot setup ──────────────────────────────────────────────────────────────

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)


# ── Helpers ────────────────────────────────────────────────────────────────

def _load_personality() -> str:
    if PERSONALITY_FILE.exists():
        return PERSONALITY_FILE.read_text(encoding="utf-8").strip()
    return "You are Berries, a playful forest demon."


def _get_context(query: str) -> str:
    collection = get_collection()
    results = collection.query(query_texts=[query], n_results=CHROMA_N_RESULTS)
    docs = results.get("documents", [[]])[0]
    if docs:
        return "=== RELEVANT PAST STREAM CONTEXT ===\n" + "\n\n".join(docs)
    return ""


# ── Events ─────────────────────────────────────────────────────────────────

@bot.event
async def on_ready() -> None:
    print(f"[discord_bot] Logged in as {bot.user} (id: {bot.user.id})")
    print(f"[discord_bot] Watching channel IDs: {DISCORD_BERRIES_CHANNEL_IDS}")


@bot.event
async def on_message(message: discord.Message) -> None:
    # Ignore messages from the bot itself
    if message.author == bot.user:
        return

    # Only respond in designated Berries channels
    if DISCORD_BERRIES_CHANNEL_IDS and message.channel.id not in DISCORD_BERRIES_CHANNEL_IDS:
        await bot.process_commands(message)
        return

    # Skip empty messages
    content = message.content.strip()
    if not content:
        return

    async with message.channel.typing():
        personality = _load_personality()
        context = _get_context(content)
        system_prompt = personality + (f"\n\n{context}" if context else "")

        user_message = f"{message.author.display_name}: {content}"
        response = await get_completion(system_prompt=system_prompt, user_message=user_message)

    await message.channel.send(response)
    await bot.process_commands(message)


# ── Slash commands (add more here as needed) ───────────────────────────────

@bot.tree.command(name="ping", description="Check if Berries is lurking")
async def ping(interaction: discord.Interaction) -> None:
    await interaction.response.send_message("*stares from the shadows* ...yes, I am here. :3")


# ── Entry point ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if not DISCORD_TOKEN:
        raise RuntimeError("DISCORD_TOKEN is not set. Check your .env file.")
    bot.run(DISCORD_TOKEN)
