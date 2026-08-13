#!/usr/bin/env python3
"""
scripts/check_prompt_budget.py

Validate shared/budget.py::estimate_prompt_tokens against reality.

The estimator exists because our token count and the backend's disagree: we
count with tiktoken/cl100k_base, the server counts with the served model's
tokenizer and then adds chat-template framing we never see. The estimator
corrects for that gap with PROMPT_BUDGET_OVERHEAD_RATIO and
PROMPT_BUDGET_PER_MESSAGE, and it is only trustworthy if it *never*
underestimates -- an underestimate is exactly the case that reaches vLLM as a
400 and loses the response.

This replays every logged chat_response call that recorded both its prompt
(traces carry the full system prompt + messages) and the backend's reported
prompt_tokens, and reports how the estimator did. Run it after changing the
model, the chat template, the tokenizer, or the prompt structure:

    python scripts/check_prompt_budget.py [--traces logs/traces]

Non-zero exit means the estimator underestimates on real traffic and the
ratio needs raising.
"""

import argparse
import glob
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from shared.budget import estimate_prompt_tokens  # noqa: E402
from shared.tokenizer import count_tokens  # noqa: E402


def load_samples(trace_dir: str) -> list[tuple[str, int, int, int]]:
    """(trace_id, raw_content_tokens, estimated_tokens, backend_reported) per call."""
    samples = []
    for path in sorted(glob.glob(os.path.join(trace_dir, "*.jsonl"))):
        with open(path) as fh:
            for line in fh:
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                data = rec.get("data") or {}
                system_prompt, messages = data.get("system_prompt"), data.get("messages")
                if not system_prompt or not isinstance(messages, list):
                    continue
                calls = [
                    c for c in rec.get("llm_calls", [])
                    if c.get("purpose") == "chat_response" and c.get("input_tokens")
                ]
                if not calls:
                    continue
                raw = count_tokens(system_prompt) + sum(
                    count_tokens(m.get("content", "")) for m in messages
                )
                samples.append((
                    rec.get("trace_id", "?"),
                    raw,
                    estimate_prompt_tokens(system_prompt, messages),
                    calls[0]["input_tokens"],
                ))
    return samples


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--traces", default="logs/traces", help="trace directory")
    args = ap.parse_args()

    samples = load_samples(args.traces)
    if not samples:
        print(f"No chat_response calls with recorded token usage under {args.traces}/")
        return 0

    under = [(t, e, a) for t, _r, e, a in samples if e < a]
    slack = [e - a for _t, _r, e, a in samples]
    raw_gap = [a - r for _t, r, _e, a in samples]

    print(f"samples: {len(samples)}")
    print(
        "raw content count vs backend: "
        f"min=+{min(raw_gap)} max=+{max(raw_gap)} mean=+{sum(raw_gap)/len(raw_gap):.1f}"
        "   (this is the gap the estimator has to cover)"
    )
    print(
        "estimator slack (estimate - actual): "
        f"min={min(slack)} max={max(slack)} mean={sum(slack)/len(slack):.1f}"
    )
    print(f"underestimates: {len(under)}/{len(samples)}")

    if under:
        worst = min(under, key=lambda x: x[1] - x[2])
        print(f"\nFAIL — estimator underestimates; worst: trace {worst[0]} "
              f"estimated {worst[1]} vs actual {worst[2]} ({worst[1] - worst[2]})")
        print("Raise PROMPT_BUDGET_OVERHEAD_RATIO until this is empty.")
        return 1

    print("\nOK — estimator never underestimates on this sample.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
