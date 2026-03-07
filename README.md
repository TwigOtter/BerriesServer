# Berries Server

**Linux box | Python | Separate services | systemd-managed**

AI chatbot powering **Berries**, a spooky forest demon who responds in Twitch chat and Discord, with persistent memory built from stream transcripts.

---

## Architecture

```
[Streamer.bot]
     |
     | HTTP POST (chat, speech, stream events, mentions)
     v
[ingest_api]  ── FastAPI, port 8000
     |── writes ──────> data/transcripts/YYYY-MM-DD.jsonl    (ground truth)
     |── embeds ──────> data/chromadb/                       (semantic index)
     |── caches ──────> deque(maxlen=2)                      (short-term memory)
     |── upserts ─────> data/users.db                        (user profiles)
     |── responds ────> Streamer.bot webhook                 (Berries' replies)

[discord_bot]  ── discord.py, persistent WebSocket
     |── reads ───────> data/chromadb/
     |── reads ───────> berries_bot/personality.txt
     |── responds ────> Discord channel                      (no transcript storage)
```

---

## Services

### `ingest_api` — The Front Door (port 8000)

Receives all events from Streamer.bot. Preprocesses, chunks, embeds, and responds.

| Endpoint | Trigger | Body |
|---|---|---|
| `POST /event/chat` | Every chat message | `username`, `display_name`, `message`, `subscription_tier` (0–3), `subscription_months`, `gift_sub_count`, `is_moderator` |
| `POST /event/speech` | STT transcription | `speaker`, `text` |
| `POST /event/mention` | Berries response request | `text`, `respond` (bool), `TTS` (bool), `log` (bool) |
| `POST /event/stream-update` | Title/category change | `title`, `category` |
| `POST /event/stream` | Any other Twitch event | `type`, `text` (pre-formatted by Streamer.bot) |
| `GET /health` | Status check | — |

All endpoints require `X-Secret: <INGEST_SECRET>` header.

**Flush conditions (buffer → JSONL + ChromaDB):**
- Token count ≥ `CHUNK_TOKEN_LIMIT` (default 480)
- `CHUNK_TIMEOUT_SEC` inactivity (default 5 min)

### `berries_bot` — AI Response Pipeline (port 8001)

Triggered by `/event/mention`. Builds context and calls LLM.

**Per-response context assembly:**
1. Load `berries_bot/personality.txt` as system prompt
2. Query ChromaDB for `CHROMA_N_RESULTS` (default 4) semantically similar past chunks
3. Prepend last 2 chunks from `recent_chunks` deque (short-term memory)
4. Call LLM → POST response back to Streamer.bot

### `discord_bot` — Community Server Bot

Same personality + ChromaDB context as the Twitch bot. Discord messages are **not** stored back into ChromaDB or transcripts.

**Setup:** See `IMPLEMENTATION.md` → Discord Setup Checklist.

---

## Data Layer

| Store | Purpose |
|---|---|
| `data/transcripts/YYYY-MM-DD.jsonl` | Ground truth stream archive, append-only |
| `data/chromadb/` | Semantic vector index (rebuilt from `.jsonl` if corrupted) |
| `data/users.db` | User profiles, passively built from chat events |
| In-memory `deque(maxlen=2)` | Last 2 chunks for short-term context |

### Chunk Schema (`.jsonl` + ChromaDB)

```json
{
  "chunk_id": "2026-03-15T21:34:00_abc123",
  "stream_date": "2026-03-15",
  "stream_title": "Disc Golf Relaxed Vibes",
  "stream_category": "Sports",
  "start_time": "2026-03-15T21:32:00Z",
  "end_time": "2026-03-15T21:34:00Z",
  "flush_reason": "token_limit",
  "text": "[TwigOtter]: content...\n[viewer]: reply...\n[StreamEvent]: SomeStreamer raided with 42 viewers!",
  "token_count": 487,
  "speaker_summary": ["TwigOtter", "viewer", "StreamEvent"]
}
```

### User Profile Schema (`users.db`)

```sql
CREATE TABLE users (
    username            TEXT PRIMARY KEY,
    display_name        TEXT,
    nickname            TEXT,
    subscription_tier   INTEGER DEFAULT 0,  -- 0=none 1=T1 2=T2 3=T3
    subscription_months INTEGER DEFAULT 0,
    gift_sub_count      INTEGER DEFAULT 0,
    messages_sent       INTEGER DEFAULT 0,
    streams_watched     INTEGER DEFAULT 0,
    notes               TEXT DEFAULT '{}',  -- JSON blob
    first_seen          TEXT NOT NULL,
    last_seen           TEXT NOT NULL
);
```

---

## Directory Structure

```
BerriesServer/
├── berries_bot/
│   ├── main.py              # response pipeline (port 8001)
│   └── personality.txt      # Berries' system prompt
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
│   ├── transcripts/         # YYYY-MM-DD.jsonl files
│   ├── chromadb/            # ChromaDB persistence
│   └── users.db             # auto-created on first run
├── deploy/
│   ├── berries-ingest.service
│   ├── berries-bot.service
│   └── berries-discord.service
├── logs/
├── .env
├── .env.example
├── IMPLEMENTATION.md        # changelog + roadmap
└── requirements.txt
```

---

## Key Dependencies

| Package | Purpose |
|---|---|
| `fastapi` + `uvicorn` | HTTP server |
| `chromadb` | Vector DB |
| `sentence-transformers` | Local embedding model (`all-MiniLM-L6-v2`) |
| `anthropic` | Anthropic API client |
| `discord.py` | Discord bot |
| `tiktoken` | Token counting |
| `python-dotenv` | `.env` loading |
