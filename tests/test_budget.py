"""
tests/test_budget.py

Unit tests for shared/budget.py -- the sum-level prompt cap that keeps an
assembled prompt inside the backend's context ceiling.

The estimator's calibration against real traffic is NOT tested here; it is
data, not logic, and lives in scripts/check_prompt_budget.py (replays logged
traces). These tests cover the shed policy: what gets dropped, in what order,
and what is never dropped.
"""

import pytest

from shared import budget
from shared.context_providers import ContextBlock


def _render(parts: list) -> str:
    """Stand-in for format_chroma_context: one line per chunk under a header."""
    return "CONTEXT:\n" + "\n".join(parts)


def _chunks(n: int, size: int = 40) -> ContextBlock:
    parts = [f"chunk{i} " + "word " * size for i in range(n)]
    return ContextBlock(text=_render(parts), parts=parts, render=_render)


def _assemble(lead_blocks, history_turns, profile_block, final_query):
    """Mirror of ask_berries._assemble_messages."""
    messages = [{"role": "developer", "content": b.text} for b in lead_blocks]
    messages += history_turns
    if profile_block:
        messages.append({"role": "developer", "content": profile_block})
    messages.append({"role": "user", "content": final_query})
    return messages


def _turns(n: int, size: int = 20) -> list[dict]:
    return [
        {"role": "user" if i % 2 == 0 else "assistant", "content": f"turn{i} " + "word " * size}
        for i in range(n)
    ]


_PROFILE = "USER PROFILE:\nName: Twig"
_QUERY = "[Twig]: what were we talking about"


def _fit(ceiling, lead_blocks, history_turns, *, max_tokens=100, margin=0, system="SYSTEM"):
    return budget.fit_to_budget(
        system_prompt=system,
        lead_blocks=lead_blocks,
        history_turns=history_turns,
        profile_block=_PROFILE,
        final_query=_QUERY,
        max_tokens=max_tokens,
        assemble=_assemble,
        ceiling=ceiling,
        margin=margin,
    )


def _cost(lead_blocks, history_turns, system="SYSTEM") -> int:
    """What fit_to_budget will measure for this combination."""
    return budget.estimate_prompt_tokens(
        system, _assemble(lead_blocks, history_turns, _PROFILE, _QUERY)
    )


def _ceiling_for(lead_blocks, history_turns, *, max_tokens=100, margin=0, system="SYSTEM") -> int:
    """
    Ceiling whose budget is exactly the cost of the given combination.

    Tests state the shape they want to survive rather than a magic token count,
    so retuning the estimator's ratio doesn't silently turn a trimming test into
    a no-op (which is how the first cut of these tests passed vacuously).
    """
    return _cost(lead_blocks, history_turns, system) + max_tokens + margin


@pytest.fixture(autouse=True)
def _force_vllm_normalisation(monkeypatch):
    """
    estimate_prompt_tokens() normalises messages per backend. Pin the backend
    so these tests don't change behaviour with the developer's .env.
    """
    monkeypatch.setattr(budget, "LLM_BACKEND", "vllm")
    monkeypatch.setattr(budget, "PROMPT_BUDGET_ENABLED", True)


class TestEstimatePromptTokens:
    def test_exceeds_raw_content_count(self):
        """The estimate must sit above the naive count -- that gap is the point."""
        from shared.tokenizer import count_tokens
        messages = [{"role": "user", "content": "hello there berries"}]
        raw = count_tokens("SYSTEM") + count_tokens(messages[0]["content"])
        assert budget.estimate_prompt_tokens("SYSTEM", messages) > raw

    def test_grows_with_content(self):
        small = budget.estimate_prompt_tokens("S", [{"role": "user", "content": "hi"}])
        large = budget.estimate_prompt_tokens("S", [{"role": "user", "content": "hi " * 500}])
        assert large > small


class TestFitToBudget:
    def test_under_budget_is_untouched(self):
        blocks, turns = _fit(100_000, [_chunks(3)], _turns(6))
        assert len(blocks[0].parts) == 3
        assert len(turns) == 6

    def test_disabled_returns_input_unchanged(self, monkeypatch):
        monkeypatch.setattr(budget, "PROMPT_BUDGET_ENABLED", False)
        blocks, turns = _fit(200, [_chunks(3)], _turns(6))
        assert len(blocks[0].parts) == 3 and len(turns) == 6

    def test_no_ceiling_backend_opts_out(self, monkeypatch):
        monkeypatch.setattr(budget, "LLM_BACKEND", "anthropic")
        blocks, turns = _fit(None, [_chunks(3)], _turns(6))
        assert len(blocks[0].parts) == 3 and len(turns) == 6

    def test_sheds_retrieval_before_history(self):
        """A small overrun costs retrieval chunks and leaves history alone."""
        history = _turns(4)
        # Room for 3 of the 6 chunks, with every history turn still affordable.
        blocks, turns = _fit(_ceiling_for([_chunks(3)], history), [_chunks(6)], history)
        assert len(blocks[0].parts) < 6, "expected retrieval chunks to be shed"
        assert len(turns) == 4, "history should survive while retrieval remains"

    def test_sheds_least_relevant_chunks_first(self):
        """Docs arrive rerank-ordered, so the tail goes first."""
        original, history = _chunks(6), _turns(2)
        blocks, _turns_out = _fit(_ceiling_for([_chunks(3)], history), [original], history)
        kept = blocks[0].parts
        assert kept == original.parts[: len(kept)]

    def test_text_is_rerendered_after_shedding(self):
        history = _turns(2)
        blocks, _ = _fit(_ceiling_for([_chunks(3)], history), [_chunks(6)], history)
        assert blocks[0].text == _render(blocks[0].parts)

    def test_empty_block_is_removed_entirely(self):
        """Shedding every chunk drops the framing text with them."""
        history = _turns(1)
        blocks, _ = _fit(_ceiling_for([], history), [_chunks(8)], history)
        assert blocks == []

    def test_history_trimmed_once_retrieval_exhausted(self):
        """Retrieval is spent first; only then does history start paying."""
        history = _turns(10)
        blocks, turns = _fit(_ceiling_for([], _turns(4)), [_chunks(8)], history)
        assert blocks == []
        assert len(turns) < 10

    def test_newest_history_turn_always_survives(self):
        """A reply that lost the message it answers is broken, not degraded."""
        history = _turns(10)
        _blocks, turns = _fit(1, [_chunks(4)], history, max_tokens=0)
        assert len(turns) == 1
        assert turns[0] == history[-1]

    def test_history_trimmed_from_oldest_end(self):
        history = _turns(10)
        _blocks, turns = _fit(_ceiling_for([], _turns(4)), [], history)
        assert 1 <= len(turns) < 10
        assert turns == history[-len(turns):]

    def test_impossible_budget_warns_and_returns_minimum(self, caplog):
        """Irreducible prompt over budget is a config problem — log, don't mangle."""
        with caplog.at_level("WARNING"):
            blocks, turns = _fit(
                1, [_chunks(4)], _turns(5), max_tokens=0, system="SYSTEM " * 200,
            )
        assert blocks == []
        assert len(turns) == 1
        assert "over context budget" in caplog.text

    def test_result_actually_fits(self):
        """The whole contract: what comes back is under the budget."""
        max_tokens, margin = 100, 32
        ceiling = _ceiling_for([_chunks(2)], _turns(3), max_tokens=max_tokens, margin=margin)
        blocks, turns = _fit(
            ceiling, [_chunks(10)], _turns(12), max_tokens=max_tokens, margin=margin,
        )
        assert _cost(blocks, turns) <= ceiling - max_tokens - margin

    def test_unshrinkable_blocks_are_not_resized(self):
        """A plain string block has no parts; it can be dropped but not shrunk."""
        plain = ContextBlock(text="LORE:\n" + "fact " * 100)
        blocks, _turns_out = _fit(100_000, [plain], _turns(2))
        assert blocks[0].text == plain.text


class TestContextBlock:
    def test_plain_block_is_not_shrinkable(self):
        assert not ContextBlock(text="hi").shrinkable

    def test_block_with_parts_and_render_is_shrinkable(self):
        assert _chunks(2).shrinkable

    def test_block_with_parts_emptied_is_not_shrinkable(self):
        assert not ContextBlock(text="x", parts=[], render=_render).shrinkable

    def test_resized_rerenders_text(self):
        block = _chunks(3)
        smaller = block.resized(block.parts[:1])
        assert smaller.parts == block.parts[:1]
        assert smaller.text == _render(block.parts[:1])

    def test_resized_without_render_raises(self):
        with pytest.raises(ValueError):
            ContextBlock(text="x").resized([])
