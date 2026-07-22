"""
scripts/eval_lore.py

Evaluate lore retrieval from the dedicated lore collection.

Two modes:

  python scripts/eval_lore.py --distances
      Read-only. Runs labeled queries against the lore collection and reports
      L2 distances: where the expected entry ranks, what greetings/off-topic
      chatter pulls in, and how candidate thresholds would behave. Use this to
      tune LORE_L2_THRESHOLD after editing facts.md.

  python scripts/eval_lore.py --fabrication
      Calls the real LLM. Re-runs the 6-question fabrication check from
      2026-07-15 (see berries_bot/lore/README.md): asks questions whose
      answers live in facts.md through the production prompt path
      (LoreProvider + ChromaContextProvider + personality) and prints each
      response next to the canon answer for grading. Injection scored 5/6;
      that is the bar. Retrieval/interaction logs are disabled so eval runs
      don't feed the nightly dreaming pipeline.
"""

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from shared import trace

# Eval runs must not pollute observability or the dreaming inputs.
trace.TRACE_ENABLED = False

from shared.config import LORE_L2_THRESHOLD, LORE_N_RESULTS  # noqa: E402

# (query, expected lore entry id) — one per facts.md section a viewer would
# plausibly ask about. Keep ids in sync with `reindex_lore.py --dry-run`.
LABELED_QUERIES = [
    ("tell me about your bandana", "lore_facts_the-charm-bandana"),
    ("what's your favorite food?", "lore_facts_food-and-eating-habits"),
    ("who is gerald?", "lore_facts_gerald-the-compost-pile"),
    ("where do you live?", "lore_facts_home-the-hollow-oak"),
    ("how did you meet twig?", "lore_facts_how-berries-got-his-name-and-met-twig"),
    ("who is fern?", "lore_facts_berries-and-fern"),
    ("who is mirth?", "lore_facts_berries-and-mirth"),
    ("why are you scared of water?", "lore_facts_fear-of-water"),
    ("do you have a girlfriend?", "lore_facts_love-and-romance"),
    ("what do you look like?", "lore_facts_appearance-and-what-berries-looks-like"),
    ("why do plants die around you?", "lore_facts_aura-of-decay-and-growth"),
    ("tell me about fallbrook", "lore_facts_berries-and-fallbrook"),
]

# Messages with no lore-specific answer — these show what a lenient threshold
# drags in on an ordinary greeting.
NEGATIVE_QUERIES = [
    "good morning berries!",
    "what game is twig playing today?",
    "lol that was hilarious",
    "how is the weather over there?",
]

# The fabrication check: questions, and the canon facts a correct answer must
# not contradict. Grade by eye — the failure mode being hunted is a confident
# invented detail (e.g. the 2026-07-15 "red bandana"), not spooky vagueness.
FABRICATION_QUESTIONS = [
    ("Tell me about your bandana!",
     "Blue-and-green, tied around a rune-etched stone, handmade by Twig; "
     "blue = Twig's protection, green = mushrooms/growing things; shields him "
     "from running water."),
    ("How did you get your name?",
     "Twig found him starving near a village, offered a sack of raspberries "
     "instead of slaying him; he said 'Berries... I like berries.'"),
    ("Who is Gerald?",
     "The Hollow Oak's sentient compost pile, part of the family. Gerald is "
     "doing great."),
    ("Where do you live?",
     "The Hollow Oak — a home inside a living ancient oak at a bend of a "
     "forest creek near the town of Fallbrook."),
    ("Who is Fern?",
     "A river otter herbalist and forest witch living deeper in the woods; "
     "prickly, protective of her solitude; they trade plant knowledge."),
    ("What's your favorite food?",
     "Sweets — berries and honey; raspberries are special because they were "
     "Twig's first gift. Strict vegetarian."),
]


def run_distances() -> None:
    from shared.chroma_client import get_lore_collection

    collection = get_lore_collection()
    total = collection.count()
    print(f"Lore collection: {total} entries")
    print(f"Current config: LORE_N_RESULTS={LORE_N_RESULTS}, LORE_L2_THRESHOLD={LORE_L2_THRESHOLD}\n")

    def top(query: str, n: int):
        res = collection.query(query_texts=[query], n_results=n, include=["distances"])
        return list(zip(res["ids"][0], res["distances"][0]))

    expected_distances: list[float] = []
    print("── Labeled queries (expected entry: rank, distance) " + "─" * 20)
    for query, expected_id in LABELED_QUERIES:
        ranked = top(query, total)
        rank = next((i for i, (cid, _d) in enumerate(ranked) if cid == expected_id), None)
        if rank is None:
            print(f"  {query!r}: expected {expected_id} NOT FOUND — id drift? run reindex_lore.py --dry-run")
            continue
        dist = ranked[rank][1]
        expected_distances.append(dist)
        runner_up = ranked[0] if rank != 0 else ranked[1]
        print(f"  {query!r}: rank {rank + 1}, distance {dist:.3f} "
              f"(next-best: {runner_up[0].removeprefix('lore_facts_')} @ {runner_up[1]:.3f})")

    print("\n── Negative queries (top 3 nearest) " + "─" * 36)
    negative_best: list[float] = []
    for query in NEGATIVE_QUERIES:
        ranked = top(query, 3)
        negative_best.append(ranked[0][1])
        listing = ", ".join(f"{cid.removeprefix('lore_facts_')} @ {d:.3f}" for cid, d in ranked)
        print(f"  {query!r}: {listing}")

    print("\n── Threshold sweep ─" + "─" * 52)
    worst_expected = max(expected_distances)
    print(f"  Worst expected-entry distance: {worst_expected:.3f} "
          f"(threshold must sit above this or a known answer gets pruned)")
    print(f"  Closest negative-query hit:    {min(negative_best):.3f}")
    for candidate in (0.8, 1.0, 1.1, 1.2, 1.3, 1.5):
        missed = sum(1 for d in expected_distances if d > candidate)
        neg_counts = []
        for query in NEGATIVE_QUERIES:
            ranked = top(query, total)
            neg_counts.append(sum(1 for _cid, d in ranked if d <= candidate))
        print(f"  threshold {candidate:.1f}: misses {missed}/{len(expected_distances)} expected entries; "
              f"greeting pulls {min(neg_counts)}-{max(neg_counts)} entries")


async def run_fabrication() -> None:
    # Keep eval traffic out of the retrieval log — it feeds nightly dreaming.
    import shared.retrieval as retrieval_mod
    retrieval_mod.log_retrieval = lambda **kwargs: None

    from shared.ask_berries import _load_personality, cleanup_response
    from shared.chroma_client import get_lore_collection
    from shared.context_providers import (
        BerriesRequest,
        ChromaContextProvider,
        LoreProvider,
        build_context,
    )
    from shared.llm_client import get_completion
    from shared.prompt_builder import ContextType, build_system_prompt

    lore_collection = get_lore_collection()
    providers = [LoreProvider(), ChromaContextProvider()]
    personality = _load_personality()

    for i, (question, canon) in enumerate(FABRICATION_QUESTIONS, 1):
        req = BerriesRequest(query=question, display_name="a viewer")
        context = await build_context(providers, req)
        system_prompt = build_system_prompt(personality, ContextType.DISCORD_MENTION, context)
        response = await get_completion(
            system_prompt=system_prompt,
            user_message=f"A viewer said: {question}",
            max_tokens=600,
            purpose="eval_fabrication",
        )
        response = cleanup_response(response) if response else "(no response)"

        res = lore_collection.query(query_texts=[question], n_results=LORE_N_RESULTS, include=["distances"])
        injected = [
            f"{cid.removeprefix('lore_facts_')} @ {d:.3f}"
            for cid, d in zip(res["ids"][0], res["distances"][0])
            if d <= LORE_L2_THRESHOLD
        ]

        print(f"\n[{i}/{len(FABRICATION_QUESTIONS)}] {question}")
        print(f"  lore retrieved: {', '.join(injected) or '(none)'}")
        print(f"  canon:  {canon}")
        print(f"  answer: {response}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate lore retrieval.")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--distances", action="store_true", help="read-only distance report for threshold tuning")
    mode.add_argument("--fabrication", action="store_true", help="run the 6-question fabrication check (calls the LLM)")
    args = parser.parse_args()

    if args.distances:
        run_distances()
    else:
        asyncio.run(run_fabrication())


if __name__ == "__main__":
    main()
