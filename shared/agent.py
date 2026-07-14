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

    try:
        async with anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY) as client:
            for iteration in range(AGENT_MAX_TOOL_ITERATIONS):
                response = await client.messages.create(
                    model=ANTHROPIC_CHAT_MODEL,
                    max_tokens=max_tokens,
                    system=system_prompt,
                    messages=messages,
                    tools=tool_schemas,
                )
                if response.stop_reason != "tool_use":
                    return _text_of(response)

                messages.append({"role": "assistant", "content": response.content})
                results = []
                for block in response.content:
                    if block.type != "tool_use":
                        continue
                    tool = get_tool(block.name, tools)
                    log.info("Tool call (round %d): %s(%r)", iteration + 1, block.name, block.input)
                    if tool is None:
                        output = f"Unknown tool: {block.name}"
                    else:
                        try:
                            output = await tool.handler(**block.input)
                        except Exception as e:
                            log.exception("Tool %s failed", block.name)
                            output = f"Tool error: {e}"
                    results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": output,
                    })
                messages.append({"role": "user", "content": results})

            # Iterations exhausted — force a final answer without tools.
            log.info("Tool iterations exhausted; requesting final answer without tools")
            response = await client.messages.create(
                model=ANTHROPIC_CHAT_MODEL,
                max_tokens=max_tokens,
                system=system_prompt,
                messages=messages,
            )
            return _text_of(response)
    except Exception:
        log.exception("run_tool_loop failed")
        return None
