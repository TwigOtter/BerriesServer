"""
tests/test_trace.py

Unit tests for the per-interaction tracing module — step timing/nesting,
LLM/tool call recording, JSONL output, error capture, and no-op behaviour
outside a trace or when tracing is disabled.
"""

import asyncio
import json

import pytest

from shared import trace


@pytest.fixture(autouse=True)
def isolated_traces(tmp_path, monkeypatch):
    """Redirect trace output to a temp dir and force tracing on."""
    monkeypatch.setattr(trace, "TRACES_DIR", tmp_path)
    monkeypatch.setattr(trace, "TRACE_ENABLED", True)
    return tmp_path


def _read_traces(traces_dir) -> list[dict]:
    records = []
    for path in traces_dir.glob("*.jsonl"):
        for line in path.read_text(encoding="utf-8").splitlines():
            records.append(json.loads(line))
    return records


class TestTrace:
    def test_writes_jsonl_with_steps_and_meta(self, isolated_traces):
        with trace.trace("test_pipeline", username="twig") as t:
            with trace.step("stage_one") as s:
                s["detail"] = 42
            trace.add(response="hello")

        records = _read_traces(isolated_traces)
        assert len(records) == 1
        r = records[0]
        assert r["pipeline"] == "test_pipeline"
        assert r["ok"] is True
        assert r["meta"] == {"username": "twig"}
        assert r["data"]["response"] == "hello"
        assert r["trace_id"] == t.trace_id
        (step,) = r["steps"]
        assert step["name"] == "stage_one"
        assert step["detail"] == 42
        assert step["ms"] >= 0

    def test_nested_steps_get_dotted_names(self, isolated_traces):
        with trace.trace("test_pipeline"):
            with trace.step("outer"):
                with trace.step("inner"):
                    pass

        (r,) = _read_traces(isolated_traces)
        names = [s["name"] for s in r["steps"]]
        # Inner steps complete (and are recorded) before their parents.
        assert names == ["outer.inner", "outer"]

    def test_llm_and_tool_calls_recorded(self, isolated_traces):
        with trace.trace("test_pipeline"):
            trace.record_llm_call(
                "chat_response", "claude-sonnet-4-6", "anthropic", 123.4,
                input_tokens=100, output_tokens=20, max_tokens=80,
            )
            trace.record_tool_call("search_memories", 55.5, input={"query": "gerald"}, ok=True)

        (r,) = _read_traces(isolated_traces)
        (llm,) = r["llm_calls"]
        assert llm["purpose"] == "chat_response"
        assert llm["input_tokens"] == 100
        (tool,) = r["tool_calls"]
        assert tool["name"] == "search_memories"
        assert tool["input"] == {"query": "gerald"}

    def test_error_recorded_and_reraised(self, isolated_traces):
        with pytest.raises(ValueError):
            with trace.trace("test_pipeline"):
                raise ValueError("boom")

        (r,) = _read_traces(isolated_traces)
        assert r["ok"] is False
        assert r["error"] == "ValueError: boom"

    async def test_survives_awaits_and_to_thread(self, isolated_traces):
        def record_from_thread():
            with trace.step("thread_step"):
                pass

        with trace.trace("test_pipeline"):
            await asyncio.sleep(0)
            with trace.step("async_step"):
                await asyncio.to_thread(record_from_thread)

        (r,) = _read_traces(isolated_traces)
        names = [s["name"] for s in r["steps"]]
        assert names == ["async_step.thread_step", "async_step"]

    def test_long_fields_are_clipped(self, isolated_traces):
        with trace.trace("test_pipeline"):
            trace.add(system_prompt="x" * 100_000)

        (r,) = _read_traces(isolated_traces)
        assert len(r["data"]["system_prompt"]) < 100_000
        assert "chars clipped" in r["data"]["system_prompt"]


class TestNoOpBehaviour:
    def test_helpers_are_noops_outside_a_trace(self, isolated_traces):
        with trace.step("orphan_step") as s:
            s["detail"] = 1
        trace.add(response="ignored")
        trace.record_llm_call("chat", "model", "anthropic", 1.0)
        trace.record_tool_call("tool", 1.0)
        assert trace.current() is None
        assert _read_traces(isolated_traces) == []

    def test_disabled_tracing_writes_nothing(self, isolated_traces, monkeypatch):
        monkeypatch.setattr(trace, "TRACE_ENABLED", False)
        with trace.trace("test_pipeline") as t:
            assert t is None
            with trace.step("stage"):
                pass
        assert _read_traces(isolated_traces) == []
