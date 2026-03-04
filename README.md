# Berries Server

**Linux box | Python | Separate services | systemd-managed**

AI chatbot and stream utilities powering **Berries**, a spooky forest demon who responds in Twitch chat and Discord, with persistent memory built from stream transcripts.

---

## Implementation Status

| Component | Status | Notes |
|---|---|---|
| `ingest_api` — HTTP event ingestion | ✅ Done | Chat, speech, mention, stream-update, stream events |
| `ingest_api` — Chunking & JSONL writes | ✅ Done | Token-limit + timeout flush, overlap window |
| `ingest_api` — ChromaDB writes | ✅ Done | Embedded on every flush |
| `ingest_api` — ChromaDB context queries | ✅ Done | Injected into `_generate_response()` |
| `ingest_api` — User upsert on chat events | ✅ Done | Passive capture from Streamer.bot payload |
| `berries_bot` — LLM response pipeline | ✅ Done | Anthropic + Ollama backends |
| `berries_bot` — Personality system prompt | ✅ Done | `berries_bot/personality.txt` |
| `discord_bot` — Bot framework | ✅ Done | discord.py, responds in configured channels |
| `discord_bot` — App registration | 🔄 Next | See Discord Setup Checklist below |
| `shared/user_db.py` — User profiles | ✅ Done | `data/users.db`, passive capture |
| `shared/chroma_client.py` — ChromaDB client | ✅ Done | Singleton, local embeddings |
| Per-user context injection into prompts | 📋 Future | Pull from users.db and append to system prompt |
| `log: false` flag (suppress transcript writes) | 📋 Future | For semi-private StreamDeck conversations |
| StreamDeck organic conversation button | 📋 Future | `/event/mention` with `log: false, TTS: true` |
| Emote spam condensing | 📋 Future | `PogChamp x3` format in preprocessor |
| `stream_utils` service | ❌ Scrapped | Streamer.bot handles all Twitch events natively |

---

## Architecture

```
[Streamer.bot]
     |
     | HTTP POST (chat, speech, stream events, mentions)
     v
[ingest_api]  ── FastAPI, port 8000
     |── writes ──────> data/transcripts/YYYY-MM-DD.jsonl   (ground truth)
     |── embeds ──────> data/chromadb/                       (semantic index)
     |── caches ──────> deque(maxlen=2)                      (short-term memory)
     |── upserts ─────> data/users.db                        (user profiles)
     |── responds ────> Streamer.bot webhook                 (Berries' replies)

[discord_bot]  ── discord.py, persistent WebSocket
     |── reads ───────> data/chromadb/                       (same index)
     |── reads ───────> berries_bot/personality.txt
     |── responds ────> Discord channel                      (no transcript storage)
```

---

## Services

### `ingest_api` — The Front Door (port 8000)

Receives all events from Streamer.bot. Preprocesses, chunks, embeds, and fans out.

**Endpoints:**

| Endpoint | Trigger | Body |
|---|---|---|
| `POST /event/chat` | Every chat message | `username`, `display_name`, `message`, `subscription_tier` (0–3), `subscription_months`, `gift_sub_count`, `is_moderator` |
| `POST /event/speech` | STT transcription | `text` |
| `POST /event/mention` | Berries response request | `text`, `respond` (bool), `TTS` (bool), `log` (bool) |
| `POST /event/stream-update` | Title/category change | `title`, `category` |
| `POST /event/stream` | Any other Twitch event | `type` + event fields (see below) |
| `GET /health` | Status check | — |

**`/event/stream` supported types:**

| `type` | Extra fields |
|---|---|
| `subscription` | `username`, `tier`, `months` |
| `gift_sub` | `gifter`, `recipient`, `tier` |
| `bits` | `username`, `amount` |
| `raid` | `from_channel`, `viewer_count` |
| `first_time_chatter` | `username` |
| `prediction_start` | `title`, `outcomes` (list) |
| `prediction_lock` | `title` |
| `prediction_result` | `title`, `winner`, `total_points` |
| `poll_start` | `title`, `choices` (list) |
| `poll_end` | `title`, `winner` |

All endpoints require `X-Secret` header matching `INGEST_SECRET`.

**Flush conditions (whichever fires first):**
- Buffer token count ≥ `CHUNK_TOKEN_LIMIT` (default 480)
- `CHUNK_TIMEOUT_SEC` (default 5 min) of inactivity

---

### `berries_bot` — AI Response Pipeline (port 8001)

Triggered via `POST /event/mention` from Streamer.bot. Builds context, calls LLM, posts response.

**Context assembly per response:**
1. Load personality from `berries_bot/personality.txt`
2. Query ChromaDB for `CHROMA_N_RESULTS` (default 4) semantically similar past chunks
3. Prepend last 2 chunks from `recent_chunks` deque (short-term memory)
4. Call LLM (Anthropic or Ollama)
5. POST response back to Streamer.bot

---

### `discord_bot` — Community Server Bot

Uses same personality + ChromaDB context as the Twitch bot. Discord messages are **not** stored in ChromaDB or transcripts.

**See Discord Setup Checklist below.**

---

## Data Layer

| Store | Purpose | Format |
|---|---|---|
| `data/transcripts/YYYY-MM-DD.jsonl` | Permanent stream archive | One JSON chunk per flush, append-only |
| `data/chromadb/` | Semantic vector search index | Rebuilt from `.jsonl` if ever corrupted |
| `data/users.db` | User profiles | SQLite — see schema below |
| `deque(maxlen=2)` | Last 2 chunks, short-term context | In-memory, lives in `ingest_api` process |

### Transcript Chunk Schema (`.jsonl` and ChromaDB)

```json
{
  "chunk_id": "2026-03-15T21:34:00_abc123",
  "stream_date": "2026-03-15",
  "stream_title": "Disc Golf Relaxed Vibes",
  "stream_category": "Sports",
  "start_time": "2026-03-15T21:32:00Z",
  "end_time": "2026-03-15T21:34:00Z",
  "flush_reason": "token_limit",
  "text": "[TwigOtter]: content...\n[viewer]: response...\n[StreamEvent]: viewer123 raided with 42 viewers!",
  "token_count": 487,
  "speaker_summary": ["TwigOtter", "viewer", "StreamEvent"]
}
```

`stream_title` / `stream_category` default to `""` until Streamer.bot fires a stream-update event.

### User Profile Schema (`users.db`)

```sql
CREATE TABLE users (
    username            TEXT PRIMARY KEY,
    display_name        TEXT,
    nickname            TEXT,          -- set manually, used organically by Berries
    subscription_tier   INTEGER DEFAULT 0,  -- 0=none 1=T1 2=T2 3=T3
    subscription_months INTEGER DEFAULT 0,
    gift_sub_count      INTEGER DEFAULT 0,
    messages_sent       INTEGER DEFAULT 0,
    streams_watched     INTEGER DEFAULT 0,
    notes               TEXT DEFAULT '{}',  -- JSON, user-requested stored info
    first_seen          TEXT NOT NULL,
    last_seen           TEXT NOT NULL
);
```

---

## Discord Setup Checklist

The bot code is complete. These manual steps remain:

- [ ] Create app at discord.com/developers/applications → New Application → "Berries"
- [ ] Under **Bot**: create bot user, copy token → `.env` `DISCORD_TOKEN`
- [ ] Enable **Privileged Gateway Intents**: Server Members Intent + Message Content Intent
- [ ] Under **OAuth2 → URL Generator**: scopes = `bot` + `applications.commands`
- [ ] Permissions: Send Messages, Read Message History, View Channels, Use Slash Commands
- [ ] Use generated URL to invite Berries to the server
- [ ] Get channel IDs for Berries-designated channels → `.env` `DISCORD_BERRIES_CHANNEL_IDS`
- [ ] Test: `/ping` responds, Berries replies in configured channels
- [ ] Spec out additional Discord features before writing more code

---

## Directory Structure

```
BerriesServer/
├── berries_bot/
│   ├── main.py              # response pipeline (port 8001)
│   └── personality.txt      # Berries' character definition & system prompt
├── discord_bot/
│   └── main.py              # discord.py bot
├── ingest_api/
│   └── main.py              # FastAPI event ingestion (port 8000)
├── shared/
│   ├── chroma_client.py     # ChromaDB singleton
│   ├── config.py            # centralized config from .env
│   ├── llm_client.py        # Anthropic + Ollama abstraction
│   ├── tokenizer.py         # token counting (tiktoken)
│   └── user_db.py           # user profile SQLite CRUD
├── data/
│   ├── transcripts/         # YYYY-MM-DD.jsonl (ground truth, append-only)
│   ├── chromadb/            # ChromaDB persistence
│   └── users.db             # user profiles (auto-created)
├── deploy/
│   ├── berries-ingest.service
│   ├── berries-bot.service
│   └── berries-discord.service
├── logs/
├── .env
├── .env.example
└── requirements.txt
```

---

## Key Dependencies

| Package | Purpose |
|---|---|
| `fastapi` + `uvicorn` | HTTP server for ingest_api |
| `chromadb` | Vector DB for semantic search |
| `sentence-transformers` | Local embedding model (no data leaves the box) |
| `anthropic` | Anthropic API client |
| `discord.py` | Discord bot |
| `tiktoken` | Token counting for flush threshold |

---

## Decision Log

| Decision | Rationale |
|---|---|
| `stream_utils` service scrapped | Streamer.bot handles first-time chatters, predictions, polls, and all Twitch events natively — no need for a separate service |
| `sentence-transformers` for embeddings | Free, runs locally, no data sent to external APIs |
| Streamer.bot owns "should Berries respond?" | Handles redeems, substring checks, subscriber gating — keeps that logic in the streaming tool where it belongs |
| `/event/stream-update` not `/event/stream-start` | Streamer.bot fires an Update event on title/category changes throughout a stream, not just at start |
| Generic `/event/stream` for all Twitch events | Single webhook URL in Streamer.bot; event type discriminated by `type` field |
| Separate `users.db` from transcript chunks | User profile data is relational and long-lived; chunks are append-only time-series |
| ChromaDB rebuild from `.jsonl` | Ground truth always in flat files — ChromaDB is a derived index, recoverable |

---

## Roadmap

### Next
- [ ] Register Discord app and invite Berries to server (see checklist above)
- [ ] Configure Streamer.bot actions to send all events to ingest_api with correct payloads

### Soon
- [ ] Per-user context injection — pull `users.db` record and append to system prompt on `/event/mention`
- [ ] Implement `log: false` flag — suppress JSONL/ChromaDB writes for semi-private exchanges
- [ ] Emote spam condensing — `PogChamp PogChamp PogChamp` → `PogChamp_x3` in preprocessor

### Future
- [ ] StreamDeck organic conversation button — `/event/mention` with `log: false, TTS: true, respond: true`
- [ ] ChromaDB rebuild utility — reconstruct index from `.jsonl` files
- [ ] `berries_bot` as standalone service — currently response pipeline lives in `ingest_api`; consider extracting
- [ ] TTS routing — when `TTS: true`, route to SpeakerBot via dedicated Streamer.bot action
