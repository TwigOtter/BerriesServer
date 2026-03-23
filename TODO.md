# TODO

This document serves as a place to store ideas, stubs, to do items, and other "future work" related entries. 
If an item from this file is completed, it should be removed from the document.

## TODO Items

- Set Pronouns method
- Consolidate user profile upserts to a single shared file so that both Twitch and Discord can share methods to set user attributes like nickname pronouns, time zones, region, birthdays, and more.
- Ensure records are merged when linking Twitch and Discord.
- The OMDB movie lookup by string is a bit imprecise and sometimes people remove the wrong movie or Twig risks announcing the wrong movie for movie night.
  - Allow users to remove movies by specifiying their integer value, not by searching (which is kinda unruly)
  - Allow Twig to specify movie to watch by integer from the list, not by searching by name.
- Allow Twig/mods to blacklist movies and provide a reason
- Create a `/event/conversation` `berries_ingest` endpoint that allows Twig to have natural conversations with the Bot.
- Find out why prosody didn't work with TTS
- Discord region/city method
- Berries weather report
- Discord time zone method (tied to location/city?)
- **`/temp <value> <unit> to <unit>`** — convert between F, C, and K; pure math, no external deps.
- Standardize logging across the whole system
- Add performance monitoring to LLM calls (how long do things take?)

## Questions/Considerations

- Should Discord/Twitch IDs be the unique qualifiers, not the usernames themselves?
- Should we inject usernames into Discord messages when people tag others via `@username`?
- Can we teach Berries what emotes he's able to use?
- Should the assistant distill relevant information from chatlogs after retrieving and store it back to the ChromaDB as source: "distilled" or something to that effect?
- Should we clear items from the ChromaDB that haven't gotten hits in some amount of time?

## Big Feature Details

### Feature: RAG Summarization Pipeline

Overview

To reduce token overhead and sycophantic drift caused by noisy ChromaDB results, we introduce a summarization layer that sits between retrieval and generation. Raw chunks from Twitch, Discord, and documents are periodically distilled into clean, factual summary entries. Berries queries both raw chunks and summaries, but summaries skip the distillation step in the live pipeline.

New Source Type: summary
ChromaDB gains a fourth source type alongside twitch, discord, and document.
Summary entry metadata:
	∙	source: summary
	∙	source_chunk_ids: [list of raw chunk IDs used to generate this summary]
	∙	stale: false
	∙	generated_at: timestamp
Key rules:
	∙	Summaries are always derived from raw chunks only — never from other summaries
	∙	The summarization pipeline must explicitly filter out source: summary when gathering input material
	∙	Raw source chunks are retained after summarization; they remain available for future retrieval and re-summarization

Live Pipeline Changes
When ChromaDB results are returned during a user query:
	1.	Check the source field of each returned chunk
	2.	If source: summary → use as-is, skip distillation call
	3.	If source: twitch | discord | document → run distillation call
Distillation prompt framing:
“Given this user query, extract only factual information from these passages that would help answer it: facts about this user, channel events, running jokes, past interactions. Do not include response style, tone, or how the bot previously phrased things.”
This strips behavioral signal from the chunks and passes only informational signal to Berries, reducing pattern-locking on past response styles.
Additionally, Berries’s system prompt should include a note that RAG context is data, not a response template.

Retrieval Logging
Rather than a simple hit counter, each retrieval is logged with:
	∙	chunk_id
	∙	timestamp
	∙	query (the rewritten search query that retrieved it)
This enables smarter summarization prompts at batch time — the batch job can see what kinds of questions a chunk has been retrieved for and direct the summary accordingly.

Nightly Batch Job (3am)
Two responsibilities:
1. Summarize hot chunks
	∙	Aggregate the retrieval log for the past 24 hours
	∙	Identify raw chunks retrieved 3+ times (threshold TBD, tune empirically)
	∙	Group related chunks by topic/query pattern
	∙	For each group, run a summarization call using the retrieval queries as directional context
	∙	Write a new source: summary entry with back-references to source chunk IDs
	∙	Clear the retrieval log entries for processed chunks
2. Regenerate stale summaries
	∙	Query for all entries where stale: true
	∙	For each stale summary, retrieve its source_chunk_ids plus any new chunks that triggered the staleness flag
	∙	Re-run summarization over this full set of raw chunks
	∙	Overwrite the summary entry, reset stale: false, update generated_at

Summary Invalidation
When a new chunk is ingested:
	1.	Use the new chunk as a query against ChromaDB
	2.	Filter results to source: summary only
	3.	If any summary returns with L2 distance below a configured threshold, set stale: true on that summary
	4.	The chunk ID that triggered staleness should be noted so the regeneration step knows to include it
Threshold notes:
	∙	Start conservative (low L2 threshold — only invalidate on very close semantic matches)
	∙	Loosen if summaries are observed going stale in practice
	∙	Goal: catch genuine contradictions and extensions without over-invalidating on loosely related content
Cascading staleness: Not a current concern at this scale, but if hierarchical summaries are introduced in the future, staleness would need to propagate up the chain. Current design avoids this entirely by prohibiting summary-of-summary.

What This Solves


|Problem                                             |Solution                                                             |
|----------------------------------------------------|---------------------------------------------------------------------|
|Noisy 512-token chunks inflating token usage        |Distillation call condenses raw results before hitting Berries       |
|Sycophantic drift from behavioral patterns in chunks|Distillation prompt explicitly strips tone/style, keeps only facts   |
|High-traffic topics always retrieving raw chunks    |Summary entries serve clean consolidated info, skip distillation     |
|New info contradicting existing summaries           |Invalidation step at ingestion flags stale summaries for regeneration|
