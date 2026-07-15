"""
tests/test_agent.py

Tests for the tool-use loop (shared/agent.py) and tool registry behaviours
(shared/tools.py). The Anthropic client is faked; no network access.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import shared.tools as tools_mod
from shared.agent import run_tool_loop
from shared.tools import BerriesTool, _ping_moderators, get_tool


# ── Anthropic client fake ────────────────────────────────────────────────────

def _text_block(text: str):
    return SimpleNamespace(type="text", text=text)

def _tool_block(name: str, tool_input: dict, block_id: str = "tu_1"):
    return SimpleNamespace(type="tool_use", name=name, input=tool_input, id=block_id)

def _response(stop_reason: str, content: list):
    return SimpleNamespace(
        stop_reason=stop_reason,
        content=content,
        usage=SimpleNamespace(input_tokens=10, output_tokens=5),
    )


class _FakeAnthropic:
    """Stands in for anthropic.AsyncAnthropic; pops queued responses."""

    def __init__(self, responses: list):
        self._responses = responses
        self.calls: list[dict] = []
        self.messages = SimpleNamespace(create=self._create)

    def __call__(self, **kwargs):  # AsyncAnthropic(api_key=...)
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def _create(self, **kwargs):
        self.calls.append(kwargs)
        return self._responses.pop(0)


# ── run_tool_loop ────────────────────────────────────────────────────────────

class TestRunToolLoop:
    async def test_returns_text_without_tool_use(self):
        fake = _FakeAnthropic([_response("end_turn", [_text_block("boo!")])])
        with (
            patch("shared.agent.LLM_BACKEND", "anthropic"),
            patch("anthropic.AsyncAnthropic", fake),
        ):
            result = await run_tool_loop("system", "hello")
        assert result == "boo!"
        assert len(fake.calls) == 1

    async def test_executes_tool_and_returns_final_text(self):
        handler = AsyncMock(return_value="the rules say be kind")
        tool = BerriesTool(name="get_server_rules", description="d", handler=handler)
        fake = _FakeAnthropic([
            _response("tool_use", [_tool_block("get_server_rules", {})]),
            _response("end_turn", [_text_block("be kind, says the forest")]),
        ])
        with (
            patch("shared.agent.LLM_BACKEND", "anthropic"),
            patch("anthropic.AsyncAnthropic", fake),
        ):
            result = await run_tool_loop("system", "what are the rules?", tools=[tool])
        assert result == "be kind, says the forest"
        handler.assert_awaited_once()
        # Second call must carry the tool_result back to the model
        followup_messages = fake.calls[1]["messages"]
        assert followup_messages[-1]["content"][0]["type"] == "tool_result"
        assert followup_messages[-1]["content"][0]["content"] == "the rules say be kind"

    async def test_tool_errors_are_reported_not_raised(self):
        handler = AsyncMock(side_effect=RuntimeError("db on fire"))
        tool = BerriesTool(name="get_user_profile", description="d", handler=handler)
        fake = _FakeAnthropic([
            _response("tool_use", [_tool_block("get_user_profile", {"name": "x"})]),
            _response("end_turn", [_text_block("hmm, my memory fails me")]),
        ])
        with (
            patch("shared.agent.LLM_BACKEND", "anthropic"),
            patch("anthropic.AsyncAnthropic", fake),
        ):
            result = await run_tool_loop("system", "who is x?", tools=[tool])
        assert result == "hmm, my memory fails me"
        assert "Tool error" in fake.calls[1]["messages"][-1]["content"][0]["content"]

    async def test_iteration_cap_forces_final_answer(self):
        handler = AsyncMock(return_value="more data")
        tool = BerriesTool(name="search_memories", description="d", handler=handler)
        fake = _FakeAnthropic([
            _response("tool_use", [_tool_block("search_memories", {"query": "a"})]),
            _response("tool_use", [_tool_block("search_memories", {"query": "b"})]),
            _response("end_turn", [_text_block("final answer")]),
        ])
        with (
            patch("shared.agent.LLM_BACKEND", "anthropic"),
            patch("shared.agent.AGENT_MAX_TOOL_ITERATIONS", 2),
            patch("anthropic.AsyncAnthropic", fake),
        ):
            result = await run_tool_loop("system", "dig deep", tools=[tool])
        assert result == "final answer"
        # Final forced call must not offer tools again
        assert "tools" not in fake.calls[2]

    async def test_non_anthropic_backend_returns_none(self):
        with patch("shared.agent.LLM_BACKEND", "ollama"):
            assert await run_tool_loop("system", "hello") is None


# ── tools ────────────────────────────────────────────────────────────────────

class TestPingModerators:
    async def test_rate_limited_second_ping(self):
        send = AsyncMock(return_value=True)
        with (
            patch.object(tools_mod, "DISCORD_MOD_PING_CHANNEL_ID", 123),
            patch.object(tools_mod, "DISCORD_TOKEN", "token"),
            patch.object(tools_mod, "_send_discord_message", send),
            patch.object(tools_mod, "_last_mod_ping", 0.0),
        ):
            first = await _ping_moderators("trouble in chat")
            second = await _ping_moderators("more trouble")
        assert first == "Moderators have been notified."
        assert "already pinged recently" in second
        send.assert_awaited_once()

    async def test_unconfigured_channel_degrades(self):
        with patch.object(tools_mod, "DISCORD_MOD_PING_CHANNEL_ID", None):
            result = await _ping_moderators("anything")
        assert "not configured" in result


def test_get_tool_lookup():
    assert get_tool("search_memories") is not None
    assert get_tool("does_not_exist") is None
