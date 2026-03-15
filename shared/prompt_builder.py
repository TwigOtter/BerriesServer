"""
shared/prompt_builder.py

Assembles context-aware system prompts for Berries.
Each call site passes a ContextType that controls which response-format
instructions are appended after the core personality.
"""

from enum import Enum


class ContextType(Enum):
    TWITCH_CHAT = "twitch_chat"           # berries_bot /respond (no TTS)
    TWITCH_TTS = "twitch_tts"             # ingest_api /event/mention with TTS=True
    DISCORD_MENTION = "discord_mention"   # Discord @mention responses
    DISCORD_ANNOUNCE = "discord_announce" # Discord announcements (movie night, going-live)


_TWITCH_BASE = """\
RESPONSE INSTRUCTIONS:
- Keep messages between 100-200 characters, never exceeding 500.
- Use informal, chat-friendly language.
- NEVER include line breaks or newline characters. All output must be a single continuous line.
- NEVER use markdown formatting of any kind (no **, no __, no bullet points, no lists with dashes).
- NEVER use asterisk-formatted roleplay or emote actions (e.g. *narrows eyes*). These are read literally by TTS and sound broken.
- Limit emote use; only mirror those already present in the message.
- Avoid repetition and spamming similar phrases."""

_INSTRUCTIONS: dict[ContextType, str] = {
    ContextType.TWITCH_CHAT: _TWITCH_BASE,
    ContextType.TWITCH_TTS: _TWITCH_BASE + """
- Your response will be read aloud by Text-to-Speech. Write naturally for audio.
- You may use SSML <prosody> tags sparingly for dramatic effect (e.g. <prosody rate="slow">text</prosody>, <prosody pitch="low">text</prosody>).""",
    ContextType.DISCORD_MENTION: """\
RESPONSE INSTRUCTIONS:
- You are responding in Twig's Discord server, not in Twitch chat. Twig may not currently be streaming.
- Limited markdown support such as **bold**, _italic_, and similar formatting render correctly here.
- Write as if your voice is being read by TTS; avoid describing roleplay or emote actions (e.g. *tilts head with jerky, puppet-like movements*).
- Keep responses concise; aim for 100-200 characters, but 1-2 short paragraphs is fine if the topic warrants it.
- Do not assume or mention that Twig is currently live or streaming unless context clearly indicates it.""",
    ContextType.DISCORD_ANNOUNCE: """\
RESPONSE INSTRUCTIONS:
- You are writing a Discord announcement for the whole server.
- Make sure the announcement clearly conveys the key information (event, time, etc.) but with Berries' personality and opinions woven in.
- Markdown is allowed and encouraged — use it to make the message punchy and engaging.
- Please avoid using roleplay or emote actions (e.g. *does a little dance*), as they often come across awkwardly in announcements.
- 2-3 sentences max.""",
}


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
