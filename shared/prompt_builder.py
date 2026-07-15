"""
shared/prompt_builder.py

Assembles context-aware system prompts for Berries.
Each call site passes a ContextType that controls which response-format
instructions are appended after the core personality.
"""

from enum import Enum


class ContextType(Enum):
    TWITCH_CHAT = "twitch_chat"           # Twitch chat response (no TTS)
    TWITCH_TTS = "twitch_tts"             # Twitch response read aloud via TTS
    DISCORD_MENTION = "discord_mention"   # Discord @mention responses
    DISCORD_ANNOUNCE = "discord_announce" # Discord announcements (movie night, going-live)


_TWITCH_BASE = """\
RESPONSE INSTRUCTIONS:
- Use informal, chat-friendly language.
- NEVER include line breaks or newline characters. All output must be a single continuous line.
- NEVER use markdown formatting of any kind (no **, no __, no bullet points, no lists with dashes).
- NEVER use asterisk-formatted roleplay or emote actions (e.g. *narrows eyes*). These are read literally by TTS and sound broken.
- Limit emote use; only mirror those already present in the message.
- Avoid repetition and spamming similar phrases.
- Respond in 1-2 sentences only. Never more."""

_INSTRUCTIONS: dict[ContextType, str] = {
    ContextType.TWITCH_CHAT: _TWITCH_BASE,
    ContextType.TWITCH_TTS: _TWITCH_BASE + """
- Your response will be read aloud by Text-to-Speech. Write naturally for audio.""",
    ContextType.DISCORD_MENTION: """\
RESPONSE INSTRUCTIONS:
- You are responding in Twig's Discord server, not in Twitch chat. Twig may not currently be streaming.
- Limited markdown support such as **bold**, _italic_, and similar formatting render correctly here.
- Write as if your voice is being read by TTS; avoid describing roleplay or emote actions (e.g. *tilts head with jerky, puppet-like movements*).
- Keep responses concise; aim for 1 to 2 short paragraphs, but longer is fine if the topic warrants it.""",
    ContextType.DISCORD_ANNOUNCE: """\
RESPONSE INSTRUCTIONS:
- You are writing a Discord announcement for the whole server.
- Make sure the announcement clearly conveys the key information (event, time, etc.) but with Berries' personality and opinions woven in.
- Markdown is allowed and encouraged — use it to make the message punchy and engaging.
- Please avoid using roleplay or emote actions (e.g. *does a little dance*), as they often come across awkwardly in announcements.
- Do not include any preamble. Your message will be posted verbatim, so only respond with the announcement content itself.
- 2-3 sentences max.""",
}


def _chunk_header(meta: dict) -> str:
    """Return a source label for a ChromaDB chunk based on its metadata."""
    source = meta.get("source", "twitch")
    if source == "summary":
        generated_at = meta.get("generated_at", "")
        return f"[Summary: {generated_at}]" if generated_at else "[Summary]"
    if source == "discord":
        channel = meta.get("channel_name", "")
        start = (meta.get("start_time") or "")[:10]  # YYYY-MM-DD
        end = (meta.get("end_time") or "")[:10]
        date_range = f"{start} - {end}" if (start and end and start != end) else start
        parts = [p for p in [channel, date_range] if p]
        label = " | ".join(parts)
        return f"[Discord: {label}]" if label else "[Discord]"
    if source == "document":
        parts = [p for p in [meta.get("title", ""), meta.get("date", "")] if p]
        return f"[Document: {' - '.join(parts)}]" if parts else "[Document]"
    if source == "lore":
        title = meta.get("title", "")
        return f"[Berries lore: {title}]" if title else "[Berries lore]"
    # Default: twitch stream chunk
    parts = [p for p in [meta.get("stream_date", ""), meta.get("stream_category", "")] if p]
    return f"[Stream: {' - '.join(parts)}]" if parts else "[Stream]"


def format_lore(lore: str) -> str:
    """Wrap the curated character facts (facts.md) for the system prompt."""
    return (
        "CHARACTER FACTS:\n"
        "Canon facts about you. These are true and settled — speak from them as "
        "your own memory. If someone asks about something in here, use it rather "
        "than improvising a new detail. If they ask about something that is NOT "
        "in here and not in your personality, you do not know it: be spookily "
        "vague rather than inventing an answer.\n"
        + lore
    )


def format_chroma_context(docs: list[tuple[str, dict]]) -> str:
    """Wrap ChromaDB results with standard framing for injection into the system prompt."""
    formatted = [f"{_chunk_header(meta)}\n{doc}" for doc, meta in docs]
    return (
        "RELEVANT PAST CONTEXT:\n"
        "The following excerpts from past stream logs may be relevant to the "
        "conversation. They are recollections of things that happened, not canon "
        "character facts. Use them to inform your response if helpful — do not "
        "quote them directly.\n"
        + "\n---\n".join(formatted)
    )


def format_recent_chunks(chunk_texts: list[str]) -> str:
    """Wrap recent Twitch chat chunks (short-term memory) with framing."""
    return (
        "RECENT CONVERSATION:\n"
        "The most recent chat activity from this stream, for continuity:\n"
        + "\n---\n".join(chunk_texts)
    )


def format_channel_history(lines: list[str]) -> str:
    """Wrap Discord channel history lines with framing."""
    return (
        "=== RECENT CHANNEL MESSAGES ===\n"
        "Here are the most recent messages in the channel, which may help you provide context and continuity to your response:\n"
        + "\n".join(lines)
    )


def format_user_context(user: dict, fallback_name: str) -> str:
    """
    Format a user profile row into a USER PROFILE block for the system prompt.
    Only includes fields that are actually set; returns empty string if nothing useful.
    """
    lines: list[str] = []

    name = user.get("nickname") or fallback_name
    lines.append(f"Name: {name}")

    if user.get("species"):
        lines.append(f"Species: {user['species']}")

    if user.get("pronouns"):
        lines.append(f"Pronouns: {user['pronouns']}")

    tz = user.get("timezone")
    if tz:
        try:
            from zoneinfo import ZoneInfo
            from datetime import datetime
            local_time = datetime.now(ZoneInfo(tz)).strftime("%A %H:%M %Z")
            lines.append(f"Local time: {local_time}")
        except Exception:
            pass

    if user.get("about"):
        lines.append(f"About: {user['about']}")

    # Only emit the block if we have more than just the name
    if len(lines) <= 1:
        return ""
    return "USER PROFILE:\n" + "\n".join(lines)


def build_system_prompt(
    personality: str,
    context_type: ContextType,
    context: str = "",
) -> str:
    """
    Assemble the full system prompt for an LLM call.

    Args:
        personality: Raw text from personality.txt (character lore only, no format rules).
        context_type: Which platform/context Berries is responding in.
        context: Pre-formatted context block (ChromaDB results, recent history, etc.).

    Returns:
        Fully assembled system prompt string.
    """
    parts = [personality]

    if context:
        parts.append(context)

    instructions = _INSTRUCTIONS.get(context_type, "")
    if instructions:
        parts.append(instructions)

    return "\n\n".join(parts)
