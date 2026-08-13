"""
shared/budget.py

Prompt budget backstop: keep an assembled prompt inside the model's context
ceiling by shedding the least valuable context, instead of letting the backend
reject the whole call.

Every input to a Berries prompt already has its own cap -- history turns
(TWITCH/DISCORD_HISTORY_TOKEN_LIMIT), retrieval chunks (WINDOW_TOKEN_LIMIT,
RERANK_CANDIDATES), lore (LORE_N_RESULTS). Nothing capped their sum. On a long
conversation with fat retrieval chunks the total walked past VLLM_CONTEXT_TOKENS
and vLLM answered 400 Bad Request, so Berries silently said nothing -- there is
no partial-credit failure mode at the backend, the whole response is lost.

fit_to_budget() is the sum-level cap. It costs the fully assembled prompt and,
if it overruns, drops retrieval chunks (least relevant first), then history
turns (oldest first), until it fits. Degrading the context beats losing the
reply.

What is never shed: personality, response instructions, lore, the user profile
block, and the live query. If those alone overrun the ceiling the prompt is
structurally too big and no amount of trimming helps -- that is a config
problem, and it gets logged as a warning rather than silently mangled.
"""

import logging
from collections.abc import Callable

from shared.config import (
    LLM_BACKEND,
    PROMPT_BUDGET_ENABLED,
    PROMPT_BUDGET_MARGIN,
    PROMPT_BUDGET_OVERHEAD_RATIO,
    PROMPT_BUDGET_PER_MESSAGE,
    VLLM_CONTEXT_TOKENS,
)
from shared.llm_client import fold_developer_blocks, merge_consecutive_messages
from shared.tokenizer import count_tokens

log = logging.getLogger(__name__)


def context_ceiling() -> int | None:
    """
    Hard prompt+completion token ceiling for the active backend, or None when
    the backend's window is large enough that budgeting is pointless.

    Only vLLM is constrained in practice: it serves one model at a fixed
    --max-model-len (4096 here), and overruns are a 400, not a truncation.
    Anthropic and Ollama windows are orders of magnitude larger than anything
    these pipelines assemble, so they opt out entirely rather than pay the
    trimming cost.
    """
    if LLM_BACKEND == "vllm":
        return VLLM_CONTEXT_TOKENS
    return None


def estimate_prompt_tokens(system_prompt: str, messages: list[dict]) -> int:
    """
    Conservative estimate of the prompt tokens the backend will actually count.

    Two corrections sit on top of the raw content count, because the raw count
    is reliably *low*:

      1. Tokenizer mismatch. shared/tokenizer.py is tiktoken/cl100k_base; the
         served model is Qwen with its own vocabulary, which splits this text
         a few percent finer.
      2. Chat-template framing. Role markers and turn delimiters are added by
         the server after the content, so they appear in vLLM's prompt_tokens
         and in no string we hold.

    Measured against 160 logged chat_response calls, vLLM's prompt_tokens ran
    101-284 tokens above the raw content count (mean 146, worst-case ratio
    1.078). ratio * content + per_message * messages reproduces that with zero
    underestimates on the same sample; a flat additive margin underestimates
    14 of 160, which is exactly the case that produces a 400.

    Messages are normalised the way get_completion() will normalise them
    (developer folding per backend, then consecutive-role merging) so the
    message count matches what is actually dispatched.
    """
    dispatched = messages
    if LLM_BACKEND != "vllm":
        dispatched = fold_developer_blocks(dispatched)
    dispatched = merge_consecutive_messages(dispatched)

    content = count_tokens(system_prompt) + sum(count_tokens(m["content"]) for m in messages)
    # +1: the system prompt is its own message once dispatched.
    framed = PROMPT_BUDGET_PER_MESSAGE * (len(dispatched) + 1)
    return int(content * PROMPT_BUDGET_OVERHEAD_RATIO) + framed


def fit_to_budget(
    system_prompt: str,
    lead_blocks: list,
    history_turns: list[dict],
    profile_block: str | None,
    final_query: str,
    max_tokens: int,
    assemble: Callable[[list, list[dict], str | None, str], list[dict]],
    ceiling: int | None = None,
    margin: int = PROMPT_BUDGET_MARGIN,
) -> tuple[list, list[dict]]:
    """
    Return (lead_blocks, history_turns) trimmed to fit the context ceiling.

    Args:
        assemble: the caller's own message-assembly function, passed in so the
                  cost is measured on the exact message list that will be sent
                  (ask_berries._assemble_messages). Budgeting against a
                  reconstructed approximation of the prompt is how you get a
                  budget that is right until someone reorders the real one.
        ceiling:  override for context_ceiling(); None uses the backend's.
        margin:   tokens left free under the ceiling after the estimate.

    Shed order is least-valuable-first: retrieval chunks before history turns,
    and within each, the lowest-ranked/oldest first. Retrieval goes first
    because a third supporting excerpt is worth less to a reply than the
    conversation it is replying to. The newest history turn always survives --
    a reply with no history is degraded, a reply that has lost the message it
    answers is broken.
    """
    resolved_ceiling = ceiling if ceiling is not None else context_ceiling()
    if not PROMPT_BUDGET_ENABLED or resolved_ceiling is None:
        return lead_blocks, history_turns

    budget = resolved_ceiling - max_tokens - margin
    blocks = list(lead_blocks)
    turns = list(history_turns)

    def cost() -> int:
        return estimate_prompt_tokens(
            system_prompt, assemble(blocks, turns, profile_block, final_query)
        )

    before = cost()
    if before <= budget:
        return blocks, turns

    dropped_chunks = 0
    dropped_turns = 0

    # 1. Retrieval chunks, least relevant first. Later blocks are shed before
    #    earlier ones: the lead list is ordered most-important-first.
    for i in range(len(blocks) - 1, -1, -1):
        block = blocks[i]
        if not getattr(block, "shrinkable", False):
            continue
        parts = list(block.parts)
        while parts and cost() > budget:
            parts.pop()
            dropped_chunks += 1
            blocks[i] = block.resized(parts)
        if not parts:
            # Every chunk gone -- drop the framing text with them.
            blocks.pop(i)
        if cost() <= budget:
            break

    # 2. History turns, oldest first, keeping at least the newest.
    while len(turns) > 1 and cost() > budget:
        turns.pop(0)
        dropped_turns += 1

    after = cost()
    if after > budget:
        log.warning(
            "Prompt over context budget after trimming: %d > %d tokens "
            "(ceiling=%d, max_tokens=%d, margin=%d). Nothing left to shed -- "
            "personality + lore + profile + query alone exceed the budget; "
            "lower LORE_N_RESULTS or the response token limit.",
            after, budget, resolved_ceiling, max_tokens, margin,
        )
    else:
        log.info(
            "Prompt trimmed to fit context budget: %d -> %d tokens (budget %d) "
            "— dropped %d retrieval chunk(s), %d history turn(s)",
            before, after, budget, dropped_chunks, dropped_turns,
        )

    # Imported here: shared.trace is a cheap import, but keeping it out of the
    # module surface means budget logic stays unit-testable without a trace.
    from shared import trace
    trace.add(prompt_budget={
        "ceiling": resolved_ceiling,
        "budget": budget,
        "before": before,
        "after": after,
        "fits": after <= budget,
        "dropped_retrieval_chunks": dropped_chunks,
        "dropped_history_turns": dropped_turns,
    })
    return blocks, turns
