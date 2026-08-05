"""
tests/test_history.py

Unit tests for shared/history.py -- turn formatting and oldest-first,
whole-turn token-budget trimming. Merging consecutive same-role turns is
NOT this module's job (see shared/llm_client.py), so it isn't tested here.
"""

from shared.history import build_history_turns, format_user_turn


def test_format_user_turn_brackets_name():
    assert format_user_turn("Twig", "hello") == "[Twig]: hello"


class TestBuildHistoryTurns:
    def test_user_turns_bracketed_assistant_turns_bare(self):
        items = [
            ("user", "Twig", "hi berries"),
            ("assistant", "Berries", "hello little otter"),
        ]
        turns = build_history_turns(items, token_limit=1000)
        assert turns == [
            {"role": "user", "content": "[Twig]: hi berries"},
            {"role": "assistant", "content": "hello little otter"},
        ]

    def test_empty_items_returns_empty(self):
        assert build_history_turns([], token_limit=1000) == []

    def test_blank_text_dropped(self):
        items = [("user", "Twig", "   "), ("user", "Twig", "real message")]
        turns = build_history_turns(items, token_limit=1000)
        assert turns == [{"role": "user", "content": "[Twig]: real message"}]

    def test_trims_from_oldest_end(self):
        # Each formatted turn costs 1 "token" under this count_fn.
        items = [("user", "Twig", f"msg{i}") for i in range(5)]
        turns = build_history_turns(items, token_limit=3, count_fn=lambda s: 1)
        assert [t["content"] for t in turns] == [
            "[Twig]: msg2", "[Twig]: msg3", "[Twig]: msg4",
        ]

    def test_newest_turn_always_kept_even_if_over_budget(self):
        items = [("user", "Twig", "short"), ("user", "Twig", "this one is huge")]
        turns = build_history_turns(items, token_limit=1, count_fn=lambda s: 100)
        assert [t["content"] for t in turns] == ["[Twig]: this one is huge"]

    def test_order_preserved_oldest_to_newest(self):
        items = [("user", "A", "one"), ("assistant", "Berries", "two"), ("user", "B", "three")]
        turns = build_history_turns(items, token_limit=1000)
        assert [t["content"] for t in turns] == ["[A]: one", "two", "[B]: three"]
