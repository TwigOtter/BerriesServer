# Berries lore

Curated character facts, preferences, running jokes, and server knowledge —
kept out of `personality.txt` but still reliably available to Berries.

## How it works

| File | How Berries gets it |
|------|---------------------|
| `facts.md` | **Retrieved via ordinary vector search**, competing with transcript chunks for the same `CHROMA_N_RESULTS` slots. Indexed as `source: "lore"` by `scripts/reindex_lore.py`; run it after editing. |
| `server-rules.md` | Read on demand by the `get_server_rules()` agent tool (`shared/tools.py`). Discord only — the tool loop is Discord-only. |

## ⚠️ Interim state — this is knowingly the worse of two options

`facts.md` **is not currently injected.** `LoreProvider` still exists in
`shared/context_providers.py` but is wired into no pipeline, and the
`source: "lore"` exclusion in `chroma_client.query_chroma_multi` has been
lifted, so lore competes in the shared pool again. Per the measurement below,
that is **3/6 on the fabrication check, down from 5/6.** Expect Berries to
confidently invent character details he misses.

This was accepted deliberately (2026-07-15) for two reasons:

1. `facts.md` and `personality.txt` overlap substantially (~36% shared
   vocabulary), so injecting both duplicated content.
2. Most of `facts.md` is irrelevant to most queries — "Good morning Berries!"
   does not need "Berries and Mirth" or "The Dark Visitor".

**The planned fix is a dedicated lore-only ChromaDB query with its own top-n,
separate from the transcript pool.** That addresses the actual mechanism the
measurement identified: lore doesn't lose a similarity contest it never
enters. Pair it with a slimmed `personality.txt` carrying only the
foundational character voice.

Until then, prefer restoring injection over shipping the interim long-term —
the numbers below are unambiguous about which is more accurate.

## Design note: separate collection, not a `where` filter (2026-07-15)

The lore-only query should target its **own ChromaDB collection**, not the
shared collection with `where={"source": "lore"}`.

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

1. **Blast radius.** `reindex_lore.py` currently upserts and deletes *inside*
   the shared 9k-doc collection (see its `where={"source": "lore"}` diff), so
   every lore edit writes to the index holding all transcripts. A separate
   collection makes a rebuild `delete_collection()` + recreate, and the
   transcript index is never opened for writing. Transcripts are rebuildable
   from JSONL, but that's hours of re-embedding.
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

## Why injection beat retrieval (measured 2026-07-15)

Retrieval was the failure mode, not the fix. Measured against the live index
(2026-07-15):

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
