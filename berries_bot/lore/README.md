# Berries lore

Curated character facts, preferences, running jokes, and server knowledge that
Berries can recall through retrieval — without bloating `personality.txt`.

## How it works

- Every `*.md` file in this directory is split into one entry per `## Heading`
  section by `scripts/reindex_lore.py` and indexed into ChromaDB with
  `source: "lore"` metadata.
- At response time the normal retrieval pipeline (`shared/retrieval.py`)
  surfaces lore entries exactly like transcript chunks: a question about food
  retrieves the food entry, a question about the server rules retrieves the
  rules entry. No extra LLM calls, no code changes to add a fact.
- Lore entries are excluded from the nightly dream summarization (they are
  curated, not conversational history) and labeled `[Berries lore: ...]` in
  the prompt so the model knows the facts are canon.

## Writing entries

- Keep each `## section` self-contained and small (a few sentences) — it is
  embedded and retrieved as one unit.
- Write in third person, present tense ("Berries is...", "Berries thinks...").
- Phrase entries with the words people would ask about ("food", "eat",
  "favorite movie") so embeddings line up with real queries.

## Workflow

1. Edit or add `*.md` files here (`*.md.example` files are ignored).
2. Run `python scripts/reindex_lore.py` (add `--dry-run` to preview).
3. Done — entries are upserted in place and deleted entries are removed
   from ChromaDB.

See `facts.md.example` for the format. Rename it to `facts.md` (and write
real lore!) to activate it.
