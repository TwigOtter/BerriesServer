"""
scripts/eval_retrieval.py

Retrieval quality eval harness.

Reads a daily retrieval log (logs/daily_interactions/YYYY-MM-DD_retrievals.json
— what shared/retrieval.py actually injected into prompts) and has the assist
model judge each chunk as relevant or not to its query. Reports precision so
changes to the retrieval pipeline (rerank threshold, candidate count, rewrite
prompt, chunking) can be compared with numbers instead of vibes.

Usage:
    python scripts/eval_retrieval.py                    # today's log
    python scripts/eval_retrieval.py --date 2026-06-10  # specific day
    python scripts/eval_retrieval.py --file logs/daily_interactions/archive/2026-06-01_retrievals.json
    python scripts/eval_retrieval.py --sample 25        # judge a random sample only

Caveats:
  - The judge is the same model family as the reranker, so scores will be
    correlated with what the reranker kept. The number is still useful for
    A/B-ing config changes and for catching regressions; for an unbiased
    ceiling, spot-check a few entries by hand now and then.
  - Each judged query costs one assist-model call.
"""

import argparse
import asyncio
import json
import random
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from shared.config import ANTHROPIC_ASSIST_MODEL, LOGS_DIR
from shared.llm_client import get_completion

_INTERACTIONS_DIR = LOGS_DIR / "daily_interactions"
_CONCURRENCY = 5

_JUDGE_SYSTEM = (
    "You evaluate a retrieval system for a Twitch streamer's AI chatbot. "
    "Given a user message and the chat-log excerpts retrieved for it, judge each excerpt. "
    "Output only JSON."
)


async def _judge_entry(query: str, chunks: list[str]) -> list[bool] | None:
    """Return a relevant/not-relevant verdict per chunk, or None on failure."""
    numbered = "\n---\n".join(f"[{i}]\n{c}" for i, c in enumerate(chunks))
    prompt = (
        f"A user sent this message to the chatbot:\n\"{query}\"\n\n"
        f"These excerpts were retrieved as context for the response:\n{numbered}\n\n"
        "For each excerpt, judge whether it is genuinely useful for responding to this "
        "message (background about the user, the topic, or a referenced event). An excerpt "
        "that only shares a keyword or is generic chat noise is NOT relevant.\n\n"
        'Reply with ONLY a JSON object mapping excerpt index to true/false, e.g. {"0": true, "1": false}.'
    )
    try:
        raw = await get_completion(
            system_prompt=_JUDGE_SYSTEM,
            user_message=prompt,
            max_tokens=8 * len(chunks) + 32,
            model=ANTHROPIC_ASSIST_MODEL,
        )
        match = re.search(r"\{.*\}", raw, flags=re.DOTALL)
        verdicts = json.loads(match.group(0))
        return [bool(verdicts.get(str(i), False)) for i in range(len(chunks))]
    except Exception as e:
        print(f"  ! judge failed for {query[:60]!r}: {e}")
        return None


async def run_eval(entries: list[tuple[str, list[str]]]) -> None:
    sem = asyncio.Semaphore(_CONCURRENCY)

    async def judged(query: str, chunks: list[str]):
        async with sem:
            return query, chunks, await _judge_entry(query, chunks)

    results = await asyncio.gather(*(judged(q, c) for q, c in entries))

    total_chunks = 0
    relevant_chunks = 0
    zero_relevant_queries = 0
    judged_queries = 0

    print()
    for query, chunks, verdicts in results:
        if verdicts is None:
            continue
        judged_queries += 1
        n_rel = sum(verdicts)
        total_chunks += len(verdicts)
        relevant_chunks += n_rel
        if n_rel == 0:
            zero_relevant_queries += 1
        marks = "".join("+" if v else "-" for v in verdicts)
        print(f"  [{marks:<6}] {n_rel}/{len(verdicts)}  {query[:70]!r}")

    if not judged_queries:
        print("No entries judged.")
        return

    print()
    print(f"Queries judged:            {judged_queries}")
    print(f"Chunk precision:           {relevant_chunks}/{total_chunks} "
          f"({100 * relevant_chunks / total_chunks:.0f}%)")
    print(f"Queries with 0 relevant:   {zero_relevant_queries}/{judged_queries} "
          f"({100 * zero_relevant_queries / judged_queries:.0f}%)")
    print()
    print("Notes: chunk precision is the share of injected chunks the judge found useful.")
    print("'Queries with 0 relevant' are cases where abstaining would have been better.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Judge retrieval log quality with the assist model.")
    parser.add_argument("--date", help="YYYY-MM-DD (default: today, UTC)")
    parser.add_argument("--file", help="explicit path to a *_retrievals.json file")
    parser.add_argument("--sample", type=int, default=0, help="judge a random sample of N queries")
    args = parser.parse_args()

    if args.file:
        path = Path(args.file)
    else:
        date_str = args.date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
        path = _INTERACTIONS_DIR / f"{date_str}_retrievals.json"

    if not path.exists():
        sys.exit(f"No retrieval log at {path}")

    data: dict[str, list[str]] = json.loads(path.read_text(encoding="utf-8"))
    entries = [(q, c) for q, c in data.items() if c]
    if not entries:
        sys.exit(f"Retrieval log at {path} has no entries with chunks.")

    if args.sample and args.sample < len(entries):
        entries = random.sample(entries, args.sample)

    print(f"Judging {len(entries)} retrieval entr{'y' if len(entries) == 1 else 'ies'} from {path}")
    asyncio.run(run_eval(entries))


if __name__ == "__main__":
    main()
