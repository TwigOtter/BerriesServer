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
- **`berries_bot/`** — Config/assets only. `personality.txt` is the character prompt loaded by `shared/ask_berries.py`. `lore/*.md` holds curated character facts indexed into ChromaDB via `python scripts/reindex_lore.py` (one entry per `## section`, surfaced through normal retrieval).

### Shared Libraries (`shared/`)
- `ask_berries.py` — LLM hub; all response pipelines live here (nickname lookup, retrieval, prompt assembly, logging). Each pipeline runs inside a `shared/trace.py` trace.
- `trace.py` / `logging_setup.py` — Observability: per-interaction traces (step timings, LLM/tool calls, prompts) written to `logs/traces/*.jsonl` + one consistent root-logger config for all services. Inspect traces with `python scripts/traces.py`; see `docs/observability.md`.
- `retrieval.py` — RAG retrieval stage: query rewriting → multi-query vector search → assist-model reranking (with abstain) → retrieval logging.
- `prompt_builder.py` — Assembles system prompts from personality + context formatters + per-ContextType instructions.
- `config.py` — All config from `.env`; every service imports from here.
- `llm_client.py` — Async abstraction over Anthropic API or Ollama (swapped via `LLM_BACKEND` env var).
- `chroma_client.py` — Singleton ChromaDB client using local `nomic-ai/nomic-embed-text-v1` embeddings (8192-token limit, requires `einops`).
- `user_db.py` / `movie_db.py` — SQLite wrappers for user profiles and movie suggestions/history.

## Configuration

Copy `.env.example` to `.env`. Key variables:
- `LLM_BACKEND` — `"anthropic"` or `"ollama"`
- `ANTHROPIC_API_KEY`, `ANTHROPIC_CHAT_MODEL`, `ANTHROPIC_ASSIST_MODEL` — Claude config (chat: Sonnet 4.6 for personality calls; assist: Haiku 4.5 for query rewriting/utility tasks)
- `DISCORD_TOKEN`, `DISCORD_BERRIES_CHANNEL_WHITELIST_IDS`, `DISCORD_ANNOUNCE_CHANNEL_ID`
- `INGEST_SECRET` — shared auth header between services
- `LOCAL_TIMEZONE` (default `America/Chicago`) — calendar-day keying for daily logs, `stream_date`, transcript filenames, and dream.py's date math; absolute timestamps stay UTC
- `CHUNK_TOKEN_LIMIT=480`, `CHUNK_TIMEOUT_SEC=300`, `CHROMA_N_RESULTS=4`
- `RERANK_ENABLED=true`, `RERANK_CANDIDATES=12`, `RERANK_MIN_SCORE=5` — assist-model reranking of retrieval candidates (`shared/retrieval.py`); measure with `python scripts/eval_retrieval.py`
- `AGENT_TOOLS_ENABLED=false` — experimental tool-use loop for Discord mentions (`shared/agent.py`, `shared/tools.py`); see `docs/agent-tools.md` before enabling
- `TRACE_ENABLED=true` — per-interaction traces in `logs/traces/YYYY-MM-DD.jsonl` (step timings, LLM token usage, full prompts); inspect with `python scripts/traces.py`

## Key Design Decisions

- **JSONL transcripts are ground truth** — ChromaDB is a derived index; can be rebuilt from `data/transcripts/*.jsonl`
- **`recent_chunks` deque** — In-memory cache (maxlen=2) in ingest_api, shared with berries_bot for recency context
- **Personality in `berries_bot/personality.txt`** — Edit character prompt without code changes; responses must be TTS-friendly (no markdown, single line)
- **Discord watch channels are logged** — Messages in `DISCORD_WATCH_CHANNEL_IDS` channels are buffered and flushed to ChromaDB (same chunking logic as Twitch). Other Discord channels are not stored.
- **Streamer.bot handles response gating** — Redeems, keywords, and sub checks are managed externally

## Test Suite

- `python -m pytest` (only works on Linux machine, not in dev environment)