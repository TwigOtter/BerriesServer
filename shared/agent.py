"""
shared/agent.py

Tool-use loop for Berries responses (experimental, AGENT_TOOLS_ENABLED).

Instead of a meta-agent that writes system prompts for a second model, the
responder model itself gets tools: the personality stays static and dynamic
knowledge arrives through tool results. One model, native Anthropic tool use,
bounded iterations.

Anthropic backend only — run_tool_loop returns None when the backend is
Ollama or the loop fails entirely, and callers fall back to the plain
single-shot pipeline.

Open product decisions are tracked in docs/agent-tools.md.
"""

import logging
import time

from shared import trace
from shared.config import (
    AGENT_MAX_TOOL_ITERATIONS,
    ANTHROPIC_API_KEY,
    ANTHROPIC_CHAT_MODEL,
    LLM_BACKEND,
)
from shared.tools import DEFAULT_TOOLS, BerriesTool, get_tool

log = logging.getLogger(__name__)


def _text_of(response) -> str | None:
    parts = [block.text for block in response.content if block.type == "text"]
    text = "\n".join(parts).strip()
    return text or None


async def run_tool_loop(
    system_prompt: str,
    user_message: str,
    max_tokens: int = 600,
    tools: list[BerriesTool] | None = None,
) -> str | None:
    """
    Run an Anthropic tool-use conversation until the model produces a final
    text answer or AGENT_MAX_TOOL_ITERATIONS tool rounds have elapsed (after
    which one last call is made without tools to force an answer).

    Returns the response text, or None if the loop is unavailable/failed —
    callers should fall back to the plain pipeline.
    """
    if LLM_BACKEND != "anthropic":
        log.warning("run_tool_loop requires the anthropic backend (LLM_BACKEND=%r)", LLM_BACKEND)
        return None

    tools = tools if tools is not None else DEFAULT_TOOLS
    tool_schemas = [t.to_anthropic() for t in tools]
    messages: list[dict] = [{"role": "user", "content": user_message}]

    import anthropic

    async def _create(client, *, with_tools: bool, purpose: str):
        """One timed API round, recorded into the active trace."""
        t0 = time.perf_counter()
        kwargs = {"tools": tool_schemas} if with_tools else {}
        response = await client.messages.create(
            model=ANTHROPIC_CHAT_MODEL,
            max_tokens=max_tokens,
            system=system_prompt,
            messages=messages,
            **kwargs,
        )
        ms = (time.perf_counter() - t0) * 1000
        log.info(
            "LLM call — purpose=%s model=%s %.2fs in=%s out=%s stop=%s",
            purpose, ANTHROPIC_CHAT_MODEL, ms / 1000,
            response.usage.input_tokens, response.usage.output_tokens, response.stop_reason,
        )
        trace.record_llm_call(
            purpose=purpose, model=ANTHROPIC_CHAT_MODEL, backend="anthropic", ms=ms,
            input_tokens=response.usage.input_tokens, output_tokens=response.usage.output_tokens,
            max_tokens=max_tokens,
        )
        return response

    try:
        async with anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY) as client:
            for iteration in range(AGENT_MAX_TOOL_ITERATIONS):
                with trace.step(f"agent_round_{iteration + 1}") as s:
                    response = await _create(client, with_tools=True, purpose=f"agent_round_{iteration + 1}")
                    s["stop_reason"] = response.stop_reason
                    if response.stop_reason != "tool_use":
                        return _text_of(response)

                    messages.append({"role": "assistant", "content": response.content})
                    results = []
                    for block in response.content:
                        if block.type != "tool_use":
                            continue
                        tool = get_tool(block.name, tools)
                        log.info("Tool call (round %d): %s(%r)", iteration + 1, block.name, block.input)
                        t0 = time.perf_counter()
                        failed = False
                        if tool is None:
                            output = f"Unknown tool: {block.name}"
                            failed = True
                        else:
                            try:
                                output = await tool.handler(**block.input)
                            except Exception as e:
                                log.exception("Tool %s failed", block.name)
                                output = f"Tool error: {e}"
                                failed = True
                        tool_ms = (time.perf_counter() - t0) * 1000
                        log.info(
                            "Tool %s finished in %.2fs (%d chars)%s",
                            block.name, tool_ms / 1000, len(output), " [FAILED]" if failed else "",
                        )
                        trace.record_tool_call(
                            block.name, tool_ms,
                            input=dict(block.input), output_preview=output[:300], ok=not failed,
                        )
                        results.append({
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": output,
                        })
                    messages.append({"role": "user", "content": results})

            # Iterations exhausted — force a final answer without tools.
            log.info("Tool iterations exhausted; requesting final answer without tools")
            with trace.step("agent_final_answer"):
                response = await _create(client, with_tools=False, purpose="agent_final_answer")
            return _text_of(response)
    except Exception:
        log.exception("run_tool_loop failed")
        return None
