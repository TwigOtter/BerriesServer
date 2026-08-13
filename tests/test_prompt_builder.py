"""
tests/test_prompt_builder.py

Unit tests for shared/prompt_builder.py.
No mocks needed — build_system_prompt is a pure function.
"""

import pytest
from shared.prompt_builder import build_system_prompt, format_user_context, ContextType

PERSONALITY = "You are Berries, a spooky forest demon."


# ── ContextType instruction content ────────────────────────────────────────

def test_twitch_chat_forbids_markdown():
    result = build_system_prompt(PERSONALITY, ContextType.TWITCH_CHAT)
    assert "markdown" in result.lower()
    assert "NEVER" in result


def test_twitch_chat_forbids_asterisk_roleplay():
    result = build_system_prompt(PERSONALITY, ContextType.TWITCH_CHAT)
    assert "asterisk" in result.lower() or "*narrows eyes*" in result


def test_twitch_chat_requires_single_line():
    result = build_system_prompt(PERSONALITY, ContextType.TWITCH_CHAT)
    assert "single continuous line" in result


def test_twitch_tts_still_forbids_markdown():
    """TTS inherits all base Twitch restrictions."""
    result = build_system_prompt(PERSONALITY, ContextType.TWITCH_TTS)
    assert "markdown" in result.lower()
    assert "single continuous line" in result


def test_discord_mention_allows_markdown():
    result = build_system_prompt(PERSONALITY, ContextType.DISCORD_MENTION)
    assert "markdown" in result.lower()
    # Should not be telling Berries to avoid markdown
    assert "NEVER use markdown" not in result

def test_discord_mention_warns_about_streaming_assumption():
    result = build_system_prompt(PERSONALITY, ContextType.DISCORD_MENTION)
    assert "streaming" in result.lower() or "live" in result.lower()


def test_discord_announce_allows_markdown():
    result = build_system_prompt(PERSONALITY, ContextType.DISCORD_ANNOUNCE)
    assert "markdown" in result.lower()
    assert "NEVER use markdown" not in result


def test_discord_announce_does_not_warn_about_streaming():
    """Announcements are explicitly about streaming — no need to suppress it."""
    twitch_chat = build_system_prompt(PERSONALITY, ContextType.TWITCH_CHAT)
    announce = build_system_prompt(PERSONALITY, ContextType.DISCORD_ANNOUNCE)
    # discord_announce should not contain the "don't assume streaming" instruction
    # that discord_mention has
    assert "do not assume" not in announce.lower()


# ── Personality is always present ──────────────────────────────────────────

def test_personality_included_for_all_context_types():
    for ctx in ContextType:
        result = build_system_prompt(PERSONALITY, ctx)
        assert PERSONALITY in result, f"Personality missing for {ctx}"


# ── Context block assembly ──────────────────────────────────────────────────

def test_context_appended_when_provided():
    context = "RELEVANT PAST CONTEXT:\nSome stream transcript."
    result = build_system_prompt(PERSONALITY, ContextType.TWITCH_CHAT, context)
    assert context in result


def test_empty_context_produces_no_trailing_whitespace():
    result = build_system_prompt(PERSONALITY, ContextType.TWITCH_CHAT, "")
    assert not result.endswith("\n\n")
    assert result == result.strip()


def test_instructions_come_after_context():
    """Instructions should be last — a final reminder before the user message."""
    context = "UNIQUE_CONTEXT_MARKER"
    result = build_system_prompt(PERSONALITY, ContextType.DISCORD_MENTION, context)
    instructions_pos = result.find("RESPONSE INSTRUCTIONS")
    context_pos = result.find(context)
    assert context_pos < instructions_pos


def test_parts_separated_by_double_newline():
    context = "some context"
    result = build_system_prompt(PERSONALITY, ContextType.TWITCH_CHAT, context)
    assert "\n\n" in result


# ── USER PROFILE block ───────────────────────────────────────────────────────

def test_pronouns_default_to_they_them_when_unset():
    """
    Omitting the line let the model infer pronouns from usernames and fursonas,
    and it got them wrong. ~94% of rows have no pronouns set, so this is the
    common path, not an edge case.
    """
    block = format_user_context({"t_login": "someone"}, "someone")
    assert "Pronouns: they/them" in block


def test_stored_pronouns_win_over_the_default():
    block = format_user_context({"t_login": "teeka", "pronouns": "he/him"}, "teeka")
    assert "Pronouns: he/him" in block
    assert "they/them" not in block


def test_bare_row_still_emits_a_block():
    """A name-only row is exactly the case the pronoun line exists to cover."""
    block = format_user_context({"t_login": "someone"}, "someone")
    assert block.startswith("USER PROFILE:")
    assert "Name: someone" in block


def test_nickname_preferred_over_fallback_name():
    block = format_user_context({"nickname": "Bean"}, "missoula_mac")
    assert "Name: Bean" in block
