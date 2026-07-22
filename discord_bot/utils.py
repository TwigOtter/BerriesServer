"""
discord_bot/utils.py

Shared helpers for the Discord bot cogs.
"""

import logging
import re

log = logging.getLogger(__name__)

_TAG_RE = re.compile(
    r"""
      <@!?(?P<user_id>\d+)>            # user mention
    | <@&(?P<role_id>\d+)>             # role mention
    | <\#(?P<channel_id>\d+)>          # channel reference
    | <a?:(?P<emoji_name>\w+):\d+>     # custom emoji (animated or not)
    """,
    re.VERBOSE,
)


def resolve_discord_tags(message, bot_user=None) -> str:
    """
    Replace raw Discord tags in message.content with readable names:
    `<@id>`/`<@!id>` → @display-name (the bot itself → @BerriesTheDemon),
    `<@&id>` → @role-name, `<#id>` → #channel-name, custom emoji → :name:.
    Unresolvable tags are left as-is.

    The LLM cannot map snowflakes to people — an untranslated `<@1479…496>`
    leaves Berries unsure who is being talked about, both in live prompts
    (mention content, channel history) and in indexed watch-channel chunks.

    User resolution order: message.mentions (populated even for users who
    have since left), the guild member cache, then user_db (covers members
    Discord no longer knows about).
    """
    guild = getattr(message, "guild", None)
    mentioned = {m.id: m for m in getattr(message, "mentions", None) or []}

    def _user_name(user_id: int) -> str | None:
        if bot_user is not None and user_id == bot_user.id:
            return "@BerriesTheDemon"
        member = mentioned.get(user_id) or (guild.get_member(user_id) if guild else None)
        if member is not None:
            return f"@{member.display_name}"
        try:
            from shared.user_db import get_user_by_discord
            row = get_user_by_discord(str(user_id))
        except Exception:
            log.exception("user_db lookup failed while resolving mention <@%s>", user_id)
            row = None
        name = (row.get("nickname") or row.get("d_username")) if row else None
        return f"@{name}" if name else None

    def _sub(match: re.Match) -> str:
        if match["user_id"]:
            return _user_name(int(match["user_id"])) or match[0]
        if match["role_id"]:
            role = guild.get_role(int(match["role_id"])) if guild else None
            return f"@{role.name}" if role else match[0]
        if match["channel_id"]:
            channel = guild.get_channel(int(match["channel_id"])) if guild else None
            name = getattr(channel, "name", None)
            return f"#{name}" if name else match[0]
        return f":{match['emoji_name']}:"

    return _TAG_RE.sub(_sub, message.content)
