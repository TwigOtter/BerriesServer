# Berries lore

Curated character facts, preferences, running jokes, and server knowledge —
kept out of `personality.txt` but still reliably available to Berries.

## How it works

Not all lore files are treated the same. **`facts.md` is always injected;
everything else is on demand.**

| File | How Berries gets it |
|------|---------------------|
| `facts.md` | **Injected verbatim into every personality prompt** by `LoreProvider` (`shared/context_providers.py`), on both Twitch and Discord. No retrieval, no selection, no LLM call. |
| `server-rules.md` | Read on demand by the `get_server_rules()` agent tool (`shared/tools.py`). Discord only — the tool loop is Discord-only. |

`source: "lore"` entries are **excluded from vector search** (the `where`
filter in `chroma_client.query_chroma_multi`), so curated facts never compete
with transcript chunks for retrieval slots.

## Why facts.md is injected rather than retrieved

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

The whole file is ~2k tokens (~half a cent per response at Sonnet 4.6 pricing),
so injecting it always is cheaper than the machinery required to choose *not*
to. A model doesn't know what it doesn't know — a missing fact reads as a
detail to supply, not a gap to look up.

**Revisit if `facts.md` outgrows the prompt** — call it 10–15k tokens. At that
point selecting sections (a menu of `##` titles + a `get_lore(sections)` tool)
starts to pay for itself. At 2k it does not.

## Writing entries

- Keep each `## section` self-contained and small (a few sentences).
- Write in third person, present tense ("Berries is...", "Berries thinks...").
- Everything in `facts.md` is in **every** prompt — it is character identity,
  not trivia. Put situational or rarely-relevant knowledge in its own file
  behind a tool instead (see `server-rules.md`).
- Prose only. No bullets or bold: Twitch responses are TTS-bound and the
  prompt forbids markdown in output.

## Workflow

1. Edit `facts.md` — changes are live on the next response, **no reindex and
   no restart needed** (the file is read per request).
2. Only run `python scripts/reindex_lore.py` if you are re-enabling passive
   lore retrieval; the ChromaDB lore entries are currently indexed but
   filtered out of every query.

See `facts.md.example` for the format.
