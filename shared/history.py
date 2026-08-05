"""
shared/history.py

Formats short-term conversation history (recent Twitch chat, recent Discord
channel messages) into user/assistant turns for the `messages` list sent to
the LLM, instead of one flattened text blob.

Platform-agnostic: callers (shared/ask_berries.py) fetch (role, display_name,
text) items from whatever source fits the platform (SQL for Twitch, a live
Discord API fetch for Discord) and hand them to build_history_turns(). This
module only knows how to format and token-budget them — merging consecutive
same-role turns happens once, globally, at dispatch time in
shared/llm_client.py, since a consecutive run can also arise from a developer
block folding next to a history turn, not just from two history turns.
"""

from collections.abc import Callable

from shared.tokenizer import count_tokens

# (role, display_name, text), oldest -> newest. role is "user" or "assistant".
HistoryItem = tuple[str, str, str]


def format_user_turn(display_name: str, text: str) -> str:
    """
    '[DisplayName]: text' -- the one place the bracket convention lives.
    Also used for the final live query, so it reads consistently with the
    history turns immediately before it.
    """
    return f"[{display_name}]: {text}"


def build_history_turns(
    items: list[HistoryItem],
    token_limit: int,
    count_fn: Callable[[str], int] = count_tokens,
) -> list[dict]:
    """
    Format items into {"role", "content"} turns, trimmed from the oldest end
    at whole-turn granularity (never mid-message) until the total is within
    token_limit. The newest turn is always kept even if it alone exceeds the
    budget.

    Berries' own turns (role="assistant") are not bracket-prefixed -- he
    doesn't need to name himself. Everyone else's are.
    """
    formatted = [
        {
            "role": role,
            "content": text if role == "assistant" else format_user_turn(display_name, text),
        }
        for role, display_name, text in items
        if text and text.strip()
    ]

    kept: list[dict] = []
    total = 0
    for turn in reversed(formatted):
        turn_tokens = count_fn(turn["content"])
        if kept and total + turn_tokens > token_limit:
            break
        total += turn_tokens
        kept.append(turn)
    kept.reverse()
    return kept
