# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**Berries** is an AI chatbot backend for a spooky forest demon character that responds in Twitch chat and Discord. It uses ChromaDB (vector search over stream transcripts) to provide context-aware responses via the Anthropic API or local Ollama.

## Running Services

Two independent services:

```bash
# Activate venv first
source /opt/berries/venv/bin/activate

# Ingest API (receives events from Streamer.bot)
uvicorn ingest_api.main:app --host 0.0.0.0 --port 8000

# Discord bot
python -m discord_bot.main
```

Production uses systemd services in `deploy/` (`berries-ingest.service`, `berries-discord.service`). Units are **symlinked** into `/etc/systemd/system/`, so `deploy/` is the source of truth — but never run `systemctl disable`/`reenable` on them (it deletes the symlink). See `docs/systemd-units.md`.

```bash
sudo systemctl restart berries-ingest berries-discord
sudo journalctl -u berries-discord -f  # tail logs
```

## Architecture

### Data Flow
```
Streamer.bot → ingest_api (8000) → ChromaDB + SQLite + JSONL transcripts
                    ↓ (on /event/mention)
              shared/ask_berries.py → ChromaDB query → LLM → Streamer.bot webhook
                    ↓ (on /event/going-live)
              discord_bot (8002 webhook) → shared/ask_berries.py → LLM → Discord announcement
```

### Services
- **`ingest_api/`** — Receives all Streamer.bot events; buffers and chunks chat (~480 tokens or 5 min timeout); embeds chunks into ChromaDB; upserts user profiles; calls `ask_berries_twitch()` on mentions.
- **`discord_bot/`** — `main.py` is a slim entry point that loads feature cogs (`cogs/mention.py`, `cogs/watcher.py`, `cogs/moderation.py`, `cogs/movies.py`, `cogs/profile.py`), the going-live webhook server (`webhook.py`), and OMDb/Giphy clients (`services.py`); calls `ask_berries_discord_mention()` and `ask_berries_twitch_going_live()`.
- **`berries_bot/`** — Config/assets only. `personality.txt` is the character prompt loaded by `shared/ask_berries.py`. `lore/facts.md` holds curated character facts, retrieved by `LoreProvider` from a **dedicated lore-only ChromaDB collection** (recall-oriented: generous top-n, lenient threshold, no rerank — see `berries_bot/lore/README.md`); reindex with `python scripts/reindex_lore.py` after editing. `lore/server-rules.md` is read on demand by the `get_server_rules()` tool and never indexed as lore.

### Shared Libraries (`shared/`)
- `ask_berries.py` — LLM hub; all response pipelines live here (nickname lookup, retrieval, prompt assembly, logging). Each pipeline runs inside a `shared/trace.py` trace.
- `trace.py` / `logging_setup.py` — Observability: per-interaction traces (step timings, LLM/tool calls, prompts) written to `logs/traces/*.jsonl` + one consistent root-logger config for all services. Inspect traces with `python scripts/traces.py`; see `docs/observability.md`.
- `retrieval.py` — RAG retrieval stage: query rewriting → multi-query vector search → window selection → assist-model reranking (with abstain) → retrieval logging. Searches transcripts/summaries/Discord; lore is retrieved separately by `LoreProvider` from its own collection.
- `windowing.py` — Pre-rerank window selection: each candidate chunk is cut to its most query-relevant slice, ~1.5x `WINDOW_TOKEN_LIMIT` (sliding windows over whole chat lines, embedded and scored by L2 distance, best window merged with its better neighbor) so the injected block fits the system-prompt budget. Runs before reranking so the judge scores the text that actually gets injected, and so a full `RERANK_CANDIDATES` batch fits in one rerank prompt.
- `context_providers.py` — Composable system-prompt/developer blocks (`LoreProvider`, `ChromaContextProvider`, `UserProfileProvider`). Pipelines in `ask_berries.py` compose a "lead" list per platform (lore + retrieval, ahead of conversation history) plus `UserProfileProvider`, called separately and placed right before the final query; both platforms lead with `LoreProvider` so the prompt is the same wherever Berries is invoked. Providers return a `ContextBlock`; one that carries `parts` + `render` (currently `ChromaContextProvider`, whose parts are the rerank-ordered docs) can be shrunk chunk-by-chunk by `budget.py` instead of only dropped whole.
- `budget.py` — Sum-level prompt cap. The per-source limits (history, windowing, lore top-n) each cap one input but nothing capped their total, so a long conversation plus fat retrieval chunks overran `VLLM_CONTEXT_TOKENS` and vLLM returned 400 — losing the whole response. `fit_to_budget()` costs the assembled prompt and sheds retrieval chunks (least relevant first), then history turns (oldest first, newest always kept). Personality, lore, profile and the live query are never shed; if those alone overrun, it logs a warning rather than mangling the prompt.
- `history.py` — Formats recent conversation (Twitch: SQL via `interactions_db.get_recent_twitch_messages`; Discord: live `channel.history()` fetch) into real `user`/`assistant` turns — `[Name]: text` for humans, unprefixed for Berries' own replies — trimmed from the oldest end at whole-turn granularity to `TWITCH_HISTORY_TOKEN_LIMIT`/`DISCORD_HISTORY_TOKEN_LIMIT`. Consecutive same-role turns are merged at dispatch time in `llm_client.py`, not left to the backend.
- `prompt_builder.py` — Assembles system prompts from personality + context formatters + per-ContextType instructions. `format_user_context()` always states pronouns, defaulting to `they/them` when the row has none (~94% of rows) — omitting the line let the model infer from usernames and get it wrong. Consequence: every known user now emits a `USER PROFILE` block, ~14 tokens where there was previously none.
- `config.py` — All config from `.env`; every service imports from here.
- `llm_client.py` — Async abstraction over Anthropic API, Ollama, or a vLLM server (swapped via `LLM_BACKEND` env var). `get_completion()` takes a `messages` list that may include `role="developer"` entries at specific positions (not just up front); `fold_developer_blocks()`/`merge_consecutive_messages()` normalize that list per-backend before dispatch — vLLM sends `developer` natively, Anthropic/Ollama get it folded into the next user turn, and every backend gets consecutive same-role turns merged (Anthropic rejects those outright).
- `chroma_client.py` — Singleton ChromaDB client using local `nomic-ai/nomic-embed-text-v1` embeddings (8192-token limit, requires `einops`).
- `user_db.py` / `movie_db.py` — SQLite wrappers for user profiles and movie suggestions/history.
- `interactions_db.py` — Per-event store (`data/interactions.db`, WAL): raw Twitch events and Discord messages, dual-written alongside JSONL/Chroma (Phase 1 of `docs/sql-interaction-storage.md`). `get_recent_twitch_messages()` is the first reader (Phase 2, Twitch only) — feeds Twitch conversation history via `history.py`.

## Configuration

Copy `.env.example` to `.env`. Key variables:
- `LLM_BACKEND` — `"anthropic"`, `"ollama"`, or `"vllm"` (`VLLM_BASE_URL` + `VLLM_CHAT_MODEL` point at an OpenAI-compatible vLLM server)
- `ANTHROPIC_API_KEY`, `ANTHROPIC_CHAT_MODEL`, `ANTHROPIC_ASSIST_MODEL` — Claude config (chat: Sonnet 4.6 for personality calls; assist: Haiku 4.5 for query rewriting/utility tasks)
- `DISCORD_TOKEN`, `DISCORD_BERRIES_CHANNEL_WHITELIST_IDS`, `DISCORD_ANNOUNCE_CHANNEL_ID`
- `INGEST_SECRET` — shared auth header between services
- `LOCAL_TIMEZONE` (default `America/Chicago`) — calendar-day keying for daily logs, `stream_date`, transcript filenames, and dream.py's date math; absolute timestamps stay UTC
- `CHUNK_TOKEN_LIMIT=480`, `CHUNK_TIMEOUT_SEC=300`, `CHROMA_N_RESULTS=3`
- `CHUNK_OVERLAP_TOKENS=120` — tokens carried into the next chunk after a flush (`ingest_api/main.py`). Token-based, never time-based: an age cutoff drops the setup for a reply that lands minutes later. Must stay well under `CHUNK_TOKEN_LIMIT` (clamped at import if not) or the carried tokens alone re-trip the flush and every message emits its own near-duplicate chunk
- `DISCORD_RESPONSE_TOKENS=300`, `TWITCH_RESPONSE_TOKENS=124` — max_tokens for Berries' Discord and Twitch chat responses (`shared/ask_berries.py`)
- `LORE_COLLECTION=berries_lore`, `LORE_N_RESULTS=6`, `LORE_L2_THRESHOLD=1.5` — recall-oriented lore retrieval (`LoreProvider`); see `berries_bot/lore/README.md`
- `RERANK_ENABLED=true`, `RERANK_CANDIDATES=12`, `RERANK_MIN_SCORE=5` — assist-model reranking of retrieval candidates (`shared/retrieval.py`); measure with `python scripts/eval_retrieval.py`
- `WINDOW_ENABLED=true`, `WINDOW_TOKEN_LIMIT=200` — pre-rerank window selection (`shared/windowing.py`): shrink each candidate chunk to its best window (best window + better neighbour ≈ 1.5x the limit, so ~300 tokens at 200). Load-bearing when `RERANK_ENABLED=true` — disabling it puts `RERANK_CANDIDATES` full chunks in one rerank prompt, overflowing the 4096-token vLLM context
- `AGENT_TOOLS_ENABLED=false` — experimental tool-use loop for Discord mentions (`shared/agent.py`, `shared/tools.py`); see `docs/agent-tools.md` before enabling
- `TWITCH_HISTORY_TOKEN_LIMIT=960`, `TWITCH_HISTORY_ROW_LIMIT=200`, `DISCORD_HISTORY_TOKEN_LIMIT=1028` — conversation-history turn budgets (`shared/history.py`)
- `VLLM_CONTEXT_TOKENS=4096` — hard context ceiling of the served vLLM model; `scripts/dream.py` and `shared/budget.py` budget their prompts against it (overflow is a 400, not a truncation). Raising it past what vLLM was launched with only moves where the error happens
- `PROMPT_BUDGET_ENABLED=true`, `PROMPT_BUDGET_OVERHEAD_RATIO=1.08`, `PROMPT_BUDGET_PER_MESSAGE=8`, `PROMPT_BUDGET_MARGIN=64` — sum-level prompt cap (`shared/budget.py`), vLLM only. The overhead pair converts our tiktoken count into what the server actually charges (its own tokenizer + chat-template framing); validate against logged traces with `python scripts/check_prompt_budget.py`. The estimate is deliberately conservative — it must never come in under the backend's count, since that is exactly the case that 400s
- `TRACE_ENABLED=true` — per-interaction traces in `logs/traces/YYYY-MM-DD.jsonl` (step timings, LLM token usage, full prompts); inspect with `python scripts/traces.py`

## Key Design Decisions

- **JSONL transcripts are ground truth** — ChromaDB is a derived index; can be rebuilt from `data/transcripts/*.jsonl`. **Caveat since nightly consolidation:** `scripts/dream.py` deletes chunks once they are folded into a prose memory, so a full reindex resurrects every consolidated-away chunk and undoes that consolidation. The summaries survive (nothing rebuilds over them), but the raw material comes back alongside them.
- **Dream consolidation writes prose memories, not fact lists** — Phase 3 re-queries ChromaDB with the day's user messages, hands each query's top chunks to the assist model, and stores one recounted-memory paragraph per query (`scripts/dream.py`). The prompt is `personality-slim.txt` + `USER PROFILE` blocks for whoever speaks in those chunks (no lore — `facts.md` is ~1950 tokens against a 4096 ceiling). Speakers are matched by folding `[DisplayName]:` prefixes and DB names to alphanumerics (chunks say `Teeka#8081`, the row says `_teeka`); ambiguous folds resolve to nothing rather than guess, and an unset `pronouns` field is forced to they/them. Phase 4 upserts it and then deletes the source chunks. Deletion happens strictly after a successful upsert, and a summary is never in its own delete list; see `tests/test_upsert_pending.py`. Evaluate changes with `python scripts/dream.py --dry-run [--date YYYY-MM-DD] [--limit N]` — suppresses every side effect and prints each memory next to the chunks it would delete.
- **The prompt budget is enforced on the sum, not per source** — every context source has its own cap, but the model's ceiling applies to their total, so `shared/budget.py` is the only thing that actually guarantees a fit. It sheds retrieval before history: a supporting excerpt is worth less to a reply than the conversation being replied to. Its token estimate is intentionally pessimistic (measured ~146 tokens under the backend's count on real traffic, so `content x 1.08 + 8/message` corrects it), which means it sometimes drops a chunk from a prompt that would have fit — that trade is deliberate, because an underestimate costs the entire response. Re-derive with `scripts/check_prompt_budget.py` after any model, template, or tokenizer change.
- **Conversation history is real turns, not a flattened block** — recent Twitch/Discord messages are sent to the LLM as delineated `user`/`assistant` messages (`shared/history.py`), not one lumped developer-role blob. Twitch sources this from SQL (`interactions_db.get_recent_twitch_messages`, restart-safe); Discord still uses a live `channel.history()` fetch (see `docs/sql-interaction-storage.md`'s decision on why).
- **Personality in `berries_bot/personality.txt`** — Edit character prompt without code changes; responses must be TTS-friendly (no markdown, single line)
- **Discord watch channels are logged** — Messages in `DISCORD_WATCH_CHANNEL_IDS` channels are buffered and flushed to ChromaDB (same chunking logic as Twitch). Other Discord channels are not stored.
- **Streamer.bot handles response gating** — Redeems, keywords, and sub checks are managed externally

## Test Suite

- `python -m pytest` (only works on Linux machine, not in dev environment)