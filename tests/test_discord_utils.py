"""
tests/test_discord_utils.py

Tests for resolve_discord_tags in discord_bot/utils.py, using plain fake
objects — no discord.py connection needed.
"""

from types import SimpleNamespace


from discord_bot.utils import resolve_discord_tags

BOT = SimpleNamespace(id=99, display_name="Berries")
TWIG = SimpleNamespace(id=1, display_name="Twig")


def _message(content, mentions=(), guild=None):
    return SimpleNamespace(content=content, mentions=list(mentions), guild=guild)


def _guild(members=(), roles=(), channels=()):
    member_map = {m.id: m for m in members}
    role_map = {r.id: r for r in roles}
    channel_map = {c.id: c for c in channels}
    return SimpleNamespace(
        get_member=member_map.get,
        get_role=role_map.get,
        get_channel=channel_map.get,
    )


def test_bot_mention_becomes_berries():
    msg = _message("hey <@99> how are you", mentions=[BOT])
    assert resolve_discord_tags(msg, bot_user=BOT) == "hey @BerriesTheDemon how are you"


def test_user_mention_resolved_from_message_mentions():
    msg = _message("what do you think <@1> -- ready?", mentions=[TWIG])
    assert resolve_discord_tags(msg, bot_user=BOT) == "what do you think @Twig -- ready?"


def test_nickname_form_resolved():
    msg = _message("<@!1> hello", mentions=[TWIG])
    assert resolve_discord_tags(msg, bot_user=BOT) == "@Twig hello"


def test_falls_back_to_guild_member_cache():
    guild = _guild(members=[TWIG])
    msg = _message("ask <@1>", guild=guild)
    assert resolve_discord_tags(msg, bot_user=BOT) == "ask @Twig"


def test_falls_back_to_user_db(monkeypatch):
    monkeypatch.setattr(
        "shared.user_db.get_user_by_discord",
        lambda d_id: {"nickname": "Mentha", "d_username": "deafiementha"} if d_id == "42" else None,
    )
    msg = _message("wave to <@42>")
    assert resolve_discord_tags(msg, bot_user=BOT) == "wave to @Mentha"


def test_unresolvable_mention_left_as_is(monkeypatch):
    monkeypatch.setattr("shared.user_db.get_user_by_discord", lambda d_id: None)
    msg = _message("who is <@777>?")
    assert resolve_discord_tags(msg, bot_user=BOT) == "who is <@777>?"


def test_role_and_channel_tags():
    guild = _guild(
        roles=[SimpleNamespace(id=5, name="Motterator")],
        channels=[SimpleNamespace(id=7, name="the-raft-general")],
    )
    msg = _message("ping <@&5> in <#7>", guild=guild)
    assert resolve_discord_tags(msg, bot_user=BOT) == "ping @Motterator in #the-raft-general"


def test_custom_emoji_normalized():
    msg = _message("nice <:twiggle:1329615034371936276> and <a:dance:123>")
    assert resolve_discord_tags(msg, bot_user=BOT) == "nice :twiggle: and :dance:"


def test_plain_text_untouched():
    msg = _message("no tags here, just :3 and <notatag>")
    assert resolve_discord_tags(msg, bot_user=BOT) == "no tags here, just :3 and <notatag>"
