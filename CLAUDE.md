# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**Berries** is an AI chatbot backend for a spooky forest demon character that responds in Twitch chat and Discord. It uses ChromaDB (vector search over stream transcripts) to provide context-aware responses via the Anthropic API or local Ollama.

## Running Services

Three independent FastAPI/async services:

```bash
# Activate venv first
source /opt/berries/venv/bin/activate

# Ingest API (receives events from Streamer.bot)
uvicorn ingest_api.main:app --host 0.0.0.0 --port 8000

# Berries bot (generates LLM responses)
uvicorn berries_bot.main:app --host 127.0.0.1 --port 8001

# Discord bot
python -m discord_bot.main
```

Production uses systemd services in `deploy/` (`berries-ingest.service`, `berries-bot.service`, `berries-discord.service`).

```bash
sudo systemctl restart berries-ingest berries-bot berries-discord
sudo journalctl -u berries-bot -f  # tail logs
```

## Architecture

### Data Flow
```
Streamer.bot → ingest_api (8000) → ChromaDB + SQLite + JSONL transcripts
                    ↓ (on /event/mention)
              berries_bot (8001) → ChromaDB query → LLM → Streamer.bot webhook
                    ↓ (on /event/going-live)
              discord_bot (8002 webhook) → Discord announcement
```

### Services
- **`ingest_api/`** — Receives all Streamer.bot events; buffers and chunks chat (~480 tokens or 5 min timeout); embeds chunks into ChromaDB; upserts user profiles; triggers berries_bot on mentions.
- **`berries_bot/`** — Assembles LLM context (4 ChromaDB results + 2 recent in-memory chunks); calls LLM with `personality.txt` system prompt; posts response back to Streamer.bot.
- **`discord_bot/`** — Handles @mentions with RAG context; slash commands for movie suggestions/history/announcements; receives going-live webhooks.

### Shared Libraries (`shared/`)
- `config.py` — All config from `.env`; every service imports from here.
- `llm_client.py` — Async abstraction over Anthropic API or Ollama (swapped via `LLM_BACKEND` env var).
- `chroma_client.py` — Singleton ChromaDB client using local `all-MiniLM-L6-v2` embeddings.
- `user_db.py` / `movie_db.py` — SQLite wrappers for user profiles and movie suggestions/history.

## Configuration

Copy `.env.example` to `.env`. Key variables:
- `LLM_BACKEND` — `"anthropic"` or `"ollama"`
- `ANTHROPIC_API_KEY`, `ANTHROPIC_MODEL` — Claude config (currently Haiku 4.5)
- `DISCORD_TOKEN`, `DISCORD_BERRIES_CHANNEL_WHITELIST_IDS`, `DISCORD_ANNOUNCE_CHANNEL_ID`
- `INGEST_SECRET` — shared auth header between services
- `CHUNK_TOKEN_LIMIT=480`, `CHUNK_TIMEOUT_SEC=300`, `CHROMA_N_RESULTS=4`

## Key Design Decisions

- **JSONL transcripts are ground truth** — ChromaDB is a derived index; can be rebuilt from `data/transcripts/*.jsonl`
- **`recent_chunks` deque** — In-memory cache (maxlen=2) in ingest_api, shared with berries_bot for recency context
- **Personality in `berries_bot/personality.txt`** — Edit character prompt without code changes; responses must be TTS-friendly (no markdown, single line)
- **Discord watch channels are logged** — Messages in `DISCORD_WATCH_CHANNEL_IDS` channels are buffered and flushed to ChromaDB (same chunking logic as Twitch). Other Discord channels are not stored.
- **Streamer.bot handles response gating** — Redeems, keywords, and sub checks are managed externally

## No Test Suite

There is no automated test suite or linter configuration. Manual testing via HTTP requests to the running services.
