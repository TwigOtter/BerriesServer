# Berries lore

Curated character facts, preferences, running jokes, and server knowledge —
kept out of `personality.txt` but still reliably available to Berries.

## How it works

| File | How Berries gets it |
|------|---------------------|
| `facts.md` | **Retrieved from a dedicated lore-only ChromaDB collection** (`LORE_COLLECTION`) by `LoreProvider`, first in every pipeline's provider list. Indexed by `scripts/reindex_lore.py`; run it after editing. |
| `server-rules.md` | Read on demand by the `get_server_rules()` agent tool (`shared/tools.py`). Discord only — the tool loop is Discord-only. **Never indexed as lore** (excluded in `reindex_lore.py`). |

## Current design (2026-07-22): dedicated collection, recall-oriented

Lore has its own ChromaDB collection with its own slots — it never enters
the similarity contest against ~9k transcript chunks that the 2026-07-15
measurement identified as the failure mechanism. Paired with a slimmed
`personality.txt` carrying only the foundational character voice.

The retrieval knobs (`shared/config.py`) are deliberately recall-oriented,
because the failure modes are wildly asymmetric — a false positive is a
paragraph of irrelevant-but-true lore in the prompt, a false negative is a
confidently fabricated character detail:

- `LORE_N_RESULTS` (default 6) — generous top-n over ~20 entries
- `LORE_L2_THRESHOLD` (default 1.5) — lenient; prune clear non-matches only
- **No reranking** — the reranker's abstain path is precision-oriented,
  the wrong tool for this pool
- Queries are the raw message + recent conversation, **unrewritten** — the
  assist-model rewrite earns its keep against noisy transcripts; a 20-entry
  curated pool doesn't need it. The conversation query **excludes Berries'
  own messages** (`lore_context`): embedding his replies steers lore
  retrieval toward whatever devices he's already leaning on — a
  voice-narrowing feedback loop observed 2026-07-22

Validated 2026-07-22 with `scripts/eval_lore.py`:

1. **Distances (`--distances`): no separating threshold exists.** Expected
   entries span L2 0.62–1.24 ("do you have a girlfriend?" needs 1.24 to reach
   love-and-romance) while "good morning berries!" already scores 0.91 against
   comfort-and-downtime. Any threshold tight enough to filter greetings prunes
   real answers, so 1.5 deliberately admits everything — `LORE_N_RESULTS` is
   the real filter, plus the format_lore instruction to ignore off-topic
   facts. Re-run after lore edits; if the pool grows much past ~30 entries,
   revisit.
2. **Fabrication check (`--fabrication`): 6/6, up from 5/6 under injection
   and 3/6 under shared-pool retrieval.** The expected lore entry ranked
   first for every question. Two answers ("The Ledger", cloudberries) looked
   like fabrications but grep'd out as legitimate recollections from stream
   transcripts — verify against transcripts before failing an answer.

## Design note: separate collection, not a `where` filter (2026-07-15)

The lore-only query targets its **own ChromaDB collection**
(`get_lore_collection()` / `query_lore_multi()` in `shared/chroma_client.py`),
not the shared collection with `where={"source": "lore"}`.

Benchmarked against the live 9,082-doc index, per query:

| Stage | Median |
|-------|--------|
| Embed query text (nomic, local) | 14.8 ms |
| Search unfiltered (all 9,082) | 5.3 ms |
| Search `where source="lore"` (23 docs) | 18.0 ms |
| Search `where source!="lore"` | 44.1 ms |

**Filtering to 23 docs is ~3.4× slower than searching all 9,082 unfiltered.**
Chroma's `where` is a pre-filter: it scans metadata across the whole
collection to build an allowed-ID set, then searches within it, forfeiting the
HNSW index. Fewer candidates, more work.

**But speed is not the reason to choose.** Embed once and reuse the vector for
both queries and the whole difference is ~18 ms (≈20 ms separate vs ≈38 ms
filtered) against a 1–3 s LLM call — well under 1% of end-to-end latency, and
embedding is 74% of what remains. Don't pick on latency; it's noise either way.

The actual reasons:

1. **Blast radius.** Lore used to be upserted and deleted *inside* the shared
   9k-doc collection, so every lore edit wrote to the index holding all
   transcripts. With a separate collection the transcript index is never
   opened for writing by lore edits. Transcripts are rebuildable from JSONL,
   but that's hours of re-embedding.
2. **It's the mechanism fix.** Own collection + own top-n means lore never
   enters the similarity contest it was measured losing. The filter doesn't
   give you that.
3. **Independent tuning.** Lore wants a more lenient distance threshold than
   transcripts. Separate collections let you tune it; one pool forces one
   threshold.

⚠️ **Footgun:** both collections must use the identical
`nomic-embed-text-v1` embedding function. A collection created with Chroma's
default embedder raises no error — it silently returns garbage rankings.
Create it through the singleton in `shared/chroma_client.py`, never a fresh
`Client()`, and consider asserting the embedding dimensionality on startup.

## Why injection beat *shared-pool* retrieval (measured 2026-07-15)

Shared-pool retrieval was the failure mode the current design responds to.
Measured against the live index (2026-07-15):

- The ~20 lore entries reached the prompt for only about **half** the
  questions they answered — they lose the embedding similarity contest
  against ~9,000 transcript chunks. "Tell me about your bandana" retrieved
  **nothing at all**.
- On a miss Berries does **not** deflect. `personality.txt`'s "answer
  unknowable questions with spooky ambiguity" rule only fires for things
  Berries *wouldn't* know — his own bandana isn't one of those, so the model
  fills the gap confidently instead. It invented a *red* bandana; the real one
  is blue-and-green on a rune-etched stone.
- A 6-question fabrication check went from **3/6 accurate under production
  retrieval to 5/6 with the file always injected** (0/6 with neither).

The core lesson survives whatever mechanism ships: **a model doesn't know what
it doesn't know.** A missing fact reads as a detail to supply, not a gap to
look up. Any selective-lore design has to be judged on whether the right
entries actually reach the prompt — not on how much prompt it saves.

The whole file is ~2k tokens (~half a cent per response at Sonnet 4.6
pricing), so cost was never the argument for selecting entries; relevance and
overlap with `personality.txt` are.

## Writing entries

- Keep each `## section` self-contained and small (a few sentences). Each one
  is now an independent retrieval unit — it has to make sense alone, and its
  heading and body carry the embedding, so write text a user's phrasing would
  plausibly match.
- Write in third person, present tense ("Berries is...", "Berries thinks...").
- Prose only. No bullets or bold: Twitch responses are TTS-bound and the
  prompt forbids markdown in output.

## Workflow

1. Edit `facts.md`.
2. **Run `python scripts/reindex_lore.py`** — required. Edits are no longer
   live on the next response; `facts.md` is read from the ChromaDB index, not
   from disk per request. Skipping this silently serves the old text.

See `facts.md.example` for the format.
