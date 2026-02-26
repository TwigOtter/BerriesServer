# Berries Server — Architecture Design Doc

**Linux box | Python | Separate services | systemd-managed**

---

## Overview

A headless Linux server running multiple Python services that together power:

- **Berries**, an AI chatbot that roleplays in Twitch chat and responds in Discord, with persistent memory built from stream transcripts
- **Stream utilities**, including first-time chatter moderation and future Twitch prediction support
- **A Discord bot** for the existing community server

All services share a common data layer on disk. Each runs as an independent systemd process so a crash in one doesn't take down the others.

---

## Architecture Diagram

```
[Streamer.bot]
     |
     | HTTP POST (events: chat, speech, raids, etc.)
     v
[ingest_api] ── FastAPI on port 8000
     |
     |── writes to ──> transcript.jsonl  (ground truth, append-only)
     |── chunks/embeds ──> ChromaDB      (semantic search index)
     |── recent context ──> deque cache  (last 2 chunks, in-memory)
     |── triggers ──────> [berries_bot]  (when response needed)
     |── triggers ──────> [stream_utils] (moderation, etc.)

[berries_bot]
     |── queries ChromaDB for relevant past context
     |── pulls recent deque chunks for short-term memory
     |── calls OpenAI chat completions API
     |── posts response to Twitch chat via Streamer.bot or Twitch API

[discord_bot]
     |── discord.py, connects to your community server
     |── Berries responds in designated channel(s)
     |── uses same ChromaDB + personality, no transcript storage from Discord

[stream_utils]
     |── first-time chatter detection and welcome logic
     |── (future) Twitch predictions via Twitch API
```

---

## Services

### 1. `ingest_api` — The Front Door

**What it does:** Receives all events from Streamer.bot via HTTP POST, preprocesses them, manages chunking and embedding, and fans out to other services.

**Tech:** FastAPI

**Responsibilities:**
- Receive chat messages, speech-to-text transcriptions, raids, subs, and other stream events
- Preprocess chat: condense emote spam into `emoteName_x10` format, filter pure noise (bot commands, single characters, etc.)
- Maintain an in-memory buffer (list) of recent messages with timestamps
- Track token count of the buffer; flush when count approaches 512 tokens OR after 5 minutes of inactivity (whichever comes first)
- On flush: write chunk to `.jsonl`, embed and store in ChromaDB, push to `deque`, drop buffer entries older than 30 seconds, record which flush condition triggered
- Fan out relevant events to `berries_bot` and `stream_utils`

**Flush conditions (whichever fires first):**
- Buffer token count nears 512 tokens
- 5-minute inactivity timer expires

---

### 2. `berries_bot` — The AI Chatbot

**What it does:** Receives a trigger from `ingest_api` when a response is warranted, builds context, calls OpenAI, and posts to Twitch chat.

**Tech:** Python, `openai` library, ChromaDB client, `collections.deque`

**Context assembly (per response):**
1. Pull the 2 most recent chunks from the in-memory `deque` (short-term memory)
2. Query ChromaDB for 3–5 semantically similar chunks from current + past streams (long-term memory)
3. Assemble into system prompt alongside Berries' personality definition
4. Call OpenAI chat completions
5. Post response back to Twitch chat

**Notes:**
- Berries' personality/system prompt lives in a config file, not hardcoded
- ChromaDB queries can optionally filter by recency (e.g., last 6 months) if needed
- Discord responses use the same personality and ChromaDB context, but Discord messages are not stored back into ChromaDB or the transcript

---

### 3. `discord_bot` — Community Server Bot

**What it does:** Runs a Discord bot in your existing community server. Berries can respond in designated channels. Other slash commands can be added over time.

**Tech:** `discord.py`

**Responsibilities:**
- Connect persistently to Discord via discord.py's WebSocket client
- Listen for messages in designated Berries channel(s)
- When triggered, call the same context assembly + OpenAI pipeline as `berries_bot`
- Support slash commands for moderation or utilities (extensible)
- Discord context is **read-only** — Berries reads and responds but Discord messages are not written to ChromaDB or transcript files

---

### 4. `stream_utils` — Moderation & Stream Actions

**What it does:** Handles stream-specific automation separate from the AI chatbot.

**Tech:** Python, Twitch API (`twitchAPI` library or direct HTTP)

**Current scope:**
- First-time chatter detection: query a local SQLite DB of known chatters; if new, trigger a welcome message or action via Streamer.bot callback
- Log new chatters to SQLite for persistence

**Future scope:**
- Twitch Predictions: create, lock, and resolve predictions via Twitch API based on triggers from Streamer.bot or slash commands
- Additional moderation actions as needed

---

## Data Layer

All services share this on-disk data. **ChromaDB and SQLite are derived; `.jsonl` files are ground truth.**

| Store | Purpose | Format |
|---|---|---|
| `transcripts/YYYY-MM-DD.jsonl` | Permanent stream archive | One JSON object per chunk, append-only |
| `ChromaDB` | Semantic vector search index | Rebuilt from `.jsonl` if ever corrupted |
| `SQLite` | Chatter history, bot state | Structured relational data |
| In-memory `deque(maxlen=2)` | Last 2 transcript chunks for short-term context | Lives in `berries_bot` process |

### Transcript Chunk Schema (`.jsonl` and ChromaDB)

```json
{
  "chunk_id": "2025-03-15T21:34:00_001",
  "stream_date": "2025-03-15",
  "start_time": "2025-03-15T21:32:00Z",
  "end_time": "2025-03-15T21:34:00Z",
  "flush_reason": "token_limit",
  "text": "[Twig]: So I just hit a really clean hyzer on hole 7...\n[chat]: PogChamp PogChamp\n[StreamerSpeech]: And then I missed the putt completely lmao",
  "token_count": 487,
  "speaker_summary": ["Twig", "chat"]
}
```

---

## Ingestion Pipeline Detail

```
New message/event arrives at ingest_api
        |
        v
Preprocess:
  - Condense emote spam: "PogChamp PogChamp PogChamp" → "PogChamp_x3"
  - Filter: drop bot commands, single-char messages, pure noise
  - Tag speaker: [chat username] or [StreamerSpeech]
        |
        v
Append to in-memory buffer
        |
        v
Check flush conditions:
  ┌─ token count ≥ ~480? ──► FLUSH (token limit)
  └─ timer ≥ 5 min? ──────► FLUSH (timeout)
        |
        v (on flush)
1. Write chunk to transcripts/YYYY-MM-DD.jsonl  ← ground truth first
2. Embed chunk → store in ChromaDB
3. Push chunk to berries_bot deque
4. Drop buffer entries older than 30 seconds (overlap window)
5. Reset timer
```

---

## Deployment (systemd)

Each service gets its own systemd unit file. Example for `ingest_api`:

```ini
[Unit]
Description=Berries Ingest API
After=network.target

[Service]
User=berries
WorkingDirectory=/opt/berries/ingest_api
ExecStart=/opt/berries/venv/bin/python main.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Services: `berries-ingest.service`, `berries-bot.service`, `berries-discord.service`, `berries-utils.service`

---

## Directory Structure

```
/opt/berries/
├── venv/                   # shared Python virtualenv
├── shared/                 # shared utilities (embedding helpers, DB clients, config)
│   ├── config.py
│   ├── chroma_client.py
│   └── tokenizer.py
├── ingest_api/
│   └── main.py             # FastAPI app
├── berries_bot/
│   ├── main.py             # response loop
│   └── personality.txt     # Berries' system prompt
├── discord_bot/
│   └── main.py             # discord.py bot
├── stream_utils/
│   └── main.py             # moderation + Twitch API
├── data/
│   ├── transcripts/        # YYYY-MM-DD.jsonl files
│   ├── chromadb/           # ChromaDB persistence directory
│   └── stream_utils.db     # SQLite for chatter history etc.
└── logs/                   # per-service log files
```

---

## Key Dependencies

| Package | Purpose |
|---|---|
| `fastapi` + `uvicorn` | HTTP server for ingest_api |
| `chromadb` | Vector DB for semantic search |
| `openai` | Chat completions + (optionally) embeddings |
| `sentence-transformers` | Local embedding model (free alternative to OpenAI embeddings) |
| `discord.py` | Discord bot |
| `twitchAPI` | Twitch API for predictions, moderation |
| `tiktoken` | Token counting for flush threshold |
| `sqlite3` | Built-in Python, chatter history |

---

## Open Questions / Future Decisions

- **Embedding model:** `sentence-transformers` local model (free, slightly more setup).
- **Berries trigger logic:** What determines when Berries should respond? Keyword mention, random interval, every N messages? Define in config.
- **Streamer.bot → ingest_api auth:** Consider a simple shared secret header so random requests can't hit your endpoint.
- **ChromaDB rebuild script:** Worth writing a small utility that reconstructs ChromaDB from the `.jsonl` files — a one-time investment that makes the whole system recoverable.
- **Twitch predictions:** Will need OAuth token with `channel:manage:predictions` scope when you get to this.
