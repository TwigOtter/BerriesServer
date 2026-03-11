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
     |── forwards ────> discord_bot :8002                    (going-live events)

[discord_bot]  ── discord.py, persistent WebSocket + FastAPI webhook :8002
     |── reads ───────> data/chromadb/
     |── reads ───────> berries_bot/personality.txt
     |── embeds ──────> data/chromadb/                       (watch channel messages)
     |── responds ────> Discord channel
```

---

## Services

### `ingest_api` — The Front Door (port 8000)

Receives all events from Streamer.bot. Preprocesses, chunks, embeds, and responds.

| Endpoint | Trigger | Body |
|---|---|---|
| `POST /event/chat` | Every chat message | `userName`, `displayName`, `userId`, `msgId`, `message`, `messageStripped`, `emoteCount`, `role`, `bits`, `firstMessage`, `isSubscribed`, `subscriptionTier`, `monthsSubscribed`, `isVip`, `isModerator` |
| `POST /event/speech` | STT transcription | `speaker`, `text` |
| `POST /event/mention` | Berries response request | `text`, `CHAT` (bool), `TTS` (bool), `log` (bool) |
| `POST /event/stream-update` | Title/category change | `title`, `category` |
| `POST /event/stream` | Any other Twitch event | `type`, `text` (pre-formatted by Streamer.bot) |
| `POST /event/going-live` | Stream start | `title`, `category` — forwarded to discord_bot |
| `GET /health` | Status check | — |

All endpoints require `X-Secret: <INGEST_SECRET>` header.

**Flush conditions (buffer → JSONL + ChromaDB):**
- Token count ≥ `CHUNK_TOKEN_LIMIT` (default 480)
- `CHUNK_TIMEOUT_SEC` inactivity (default 5 min)

After each flush, the last `CHUNK_OVERLAP_SEC` seconds of entries (default 30s) are kept as the seed for the next chunk.

### `berries_bot` — AI Response Pipeline (port 8001)

Triggered by `/event/mention`. Builds context and calls LLM.

**Per-response context assembly:**
1. Load `berries_bot/personality.txt` as system prompt
2. Query ChromaDB for `CHROMA_N_RESULTS` (default 4) semantically similar past chunks
3. Prepend last 2 chunks from `recent_chunks` deque (short-term memory)
4. Call LLM → POST response back to Streamer.bot

### `discord_bot` — Community Server Bot

Same personality + ChromaDB context as the Twitch bot.

**@mention handling:** Responds to direct @mentions anywhere in the server. Outside whitelisted channels, redirects to `#berries-chat` after 2 bot messages in recent history to avoid flooding other channels.

**Watch channels:** Messages in `DISCORD_WATCH_CHANNEL_IDS` are buffered and flushed to ChromaDB using the same chunking logic as Twitch (token limit or inactivity). The last `DISCORD_CHUNK_OVERLAP_MESSAGES` (default 5) messages are kept as overlap after each flush.

**Stickers-only enforcement:** In `DISCORD_STICKERS_ONLY_CHANNEL_IDS`, non-sticker messages from non-mods are deleted and the rules sticker is posted.

**Slash commands:**

| Command | Access | Description |
|---|---|---|
| `/ping` | Everyone | Check if Berries is online |
| `/twitch-link <username>` | Everyone | Link your Twitch account to your Discord profile |
| `/movie suggest add <title>` | Everyone | Suggest a movie for movie night (OMDb lookup + disambiguation) |
| `/movie suggest list` | Everyone | See all current suggestions |
| `/movie suggest remove <title>` | Mods | Remove a suggestion |
| `/movie announce <title> [notes]` | Mods | Announce tonight's movie, mark as watched, post to announce channel |
| `/movie history list` | Everyone | See movies already watched |
| `/movie history remove <title>` | Mods | Remove a movie from watch history |

---

## Data Layer

| Store | Purpose |
|---|---|
| `data/transcripts/stream_chat_YYYY-MM-DD.jsonl` | Ground truth stream archive, append-only |
| `data/chromadb/` | Semantic vector index (rebuilt from `.jsonl` if corrupted) |
| `data/users.db` | User profiles, passively built from chat events |
| In-memory `deque(maxlen=2)` | Last 2 chunks for short-term context |

### Chunk Schema (`.jsonl` + ChromaDB)

```json
{
  "chunk_id": "2026-03-15T21-34-00_abc123",
  "stream_date": "2026-03-15",
  "stream_title": "Disc Golf Relaxed Vibes",
  "stream_category": "Sports",
  "start_time": "2026-03-15T21:32:00Z",
  "end_time": "2026-03-15T21:34:00Z",
  "flush_reason": "token_limit",
  "text": "[TwigOtter]: content...\n[viewer]: reply...\n[StreamEvent]: SomeStreamer raided with 42 viewers!",
  "token_count": 487,
  "source_summary": ["TwigOtter", "viewer", "StreamEvent"]
}
```

### User Profile Schema (`users.db`)

Column prefixes: `t_` = Twitch-specific, `d_` = Discord-specific, no prefix = platform-agnostic.

```sql
CREATE TABLE users (
    id                    TEXT PRIMARY KEY,    -- internal UUID, never changes
    t_id                  INTEGER,             -- Twitch numeric user ID (stable platform key)
    t_login               TEXT NOT NULL,       -- lowercase Twitch login, e.g. "twigotter"
    t_display_name        TEXT,                -- case-preserved, e.g. "TwigOtter"
    t_past_logins         TEXT DEFAULT '[]',   -- JSON list of previous logins (rename history)
    t_subscription_tier   INTEGER DEFAULT 0,   -- 0=none 1=T1 2=T2 3=T3
    t_subscription_months INTEGER DEFAULT 0,
    t_gift_sub_count      INTEGER DEFAULT 0,
    t_messages_sent       INTEGER DEFAULT 0,
    t_streams_watched     INTEGER DEFAULT 0,
    d_id                  TEXT,                -- Discord snowflake ID (stable platform key)
    d_username            TEXT,                -- current Discord username
    d_past_usernames      TEXT DEFAULT '[]',   -- JSON list of previous Discord usernames
    nickname              TEXT,                -- what Berries calls them (cross-platform)
    pronouns              TEXT,                -- e.g. "she/her", "they/them"
    species               TEXT,                -- e.g. "red fox", "border collie"
    timezone              TEXT,                -- IANA format, e.g. "America/New_York"
    birthday              TEXT,                -- MM-DD only, no year
    country               TEXT,
    notes                 TEXT DEFAULT '{}',   -- JSON blob for ad-hoc observations
    first_seen            TEXT NOT NULL,       -- ISO timestamp
    last_seen             TEXT NOT NULL        -- ISO timestamp
);
```

`t_id` is used as the stable Twitch identity. When a user renames, their old login is appended to `t_past_logins` and `t_login` is updated in place. Discord accounts are linked via `/twitch-link` and stored in `d_id`/`d_username`.

---

## Configuration

Copy `.env.example` to `.env`. Key variables:

| Variable | Default | Description |
|---|---|---|
| `LLM_BACKEND` | `anthropic` | `"anthropic"` or `"ollama"` |
| `ANTHROPIC_API_KEY` | — | Claude API key |
| `ANTHROPIC_MODEL` | — | e.g. `claude-haiku-4-5-20251001` |
| `OLLAMA_BASE_URL` | `http://localhost:11434` | Ollama endpoint |
| `OLLAMA_MODEL` | — | e.g. `llama3.1` |
| `INGEST_SECRET` | — | Shared auth header between Streamer.bot and all services |
| `STREAMERBOT_CALLBACK_URL` | — | Where Berries POSTs her replies |
| `TWITCH_CHANNEL` | `twigotter` | Channel name for going-live announcement links |
| `DISCORD_TOKEN` | — | Discord bot token |
| `DISCORD_BERRIES_CHANNEL_WHITELIST_IDS` | — | Comma-separated channel IDs; redirect check skipped here |
| `DISCORD_WATCH_CHANNEL_IDS` | — | Comma-separated channel IDs; messages are chunked and embedded |
| `DISCORD_BERRIES_CHAT_CHANNEL_ID` | — | Dedicated Berries conversation channel |
| `DISCORD_ANNOUNCE_CHANNEL_ID` | — | Channel for going-live and movie night announcements |
| `DISCORD_LOG_CHANNEL_ID` | — | Channel for bot admin logs (Twitch links, etc.) |
| `DISCORD_BOT_WEBHOOK_PORT` | `8002` | Port for discord_bot's internal webhook server |
| `DISCORD_BOT_WEBHOOK_URL` | `http://127.0.0.1:8002` | Used by ingest_api to forward going-live events |
| `DISCORD_EVENT_ROLE_ID` | — | Role pinged for movie night announcements |
| `DISCORD_STREAM_ROLE_ID` | — | Role pinged for going-live announcements |
| `DISCORD_STICKERS_ONLY_CHANNEL_IDS` | — | Comma-separated; non-sticker messages deleted |
| `DISCORD_RULES_STICKER_ID` | — | Sticker posted after deleting a violation |
| `OMDB_API_KEY` | — | OMDb API key (free at omdbapi.com) for movie lookups |
| `GIPHY_API_KEY` | — | Giphy API key for announcement GIFs |
| `CHUNK_TOKEN_LIMIT` | `480` | Flush buffer at this many tokens |
| `CHUNK_TIMEOUT_SEC` | `300` | Flush buffer after this many seconds of inactivity |
| `CHUNK_OVERLAP_SEC` | `30` | Seconds of Twitch messages to carry over after a flush |
| `DISCORD_CHUNK_OVERLAP_MESSAGES` | `5` | Discord messages to carry over after a flush |
| `CHROMA_N_RESULTS` | `4` | ChromaDB results per query |
| `EMBEDDING_MODEL` | `all-MiniLM-L6-v2` | Local sentence-transformers model |

---

## Directory Structure

```
BerriesServer/
├── berries_bot/
│   ├── main.py              # response pipeline (port 8001)
│   └── personality.txt      # Berries' system prompt
├── discord_bot/
│   └── main.py              # discord.py bot + FastAPI webhook (port 8002)
├── ingest_api/
│   └── main.py              # FastAPI event ingestion (port 8000)
├── shared/
│   ├── chroma_client.py     # ChromaDB singleton
│   ├── config.py            # centralized config from .env
│   ├── llm_client.py        # Anthropic + Ollama abstraction
│   ├── movie_db.py          # movie suggestions/history SQLite CRUD
│   ├── tokenizer.py         # token counting (tiktoken)
│   └── user_db.py           # user profile SQLite CRUD
├── data/
│   ├── transcripts/         # stream_chat_YYYY-MM-DD.jsonl files
│   ├── chromadb/            # ChromaDB persistence
│   └── users.db             # auto-created on first run
├── deploy/
│   ├── berries-ingest.service
│   ├── berries-bot.service
│   └── berries-discord.service
├── documentation/
│   └── streamerbot-setup-checklist.md
├── HelperMethods/
│   └── live_transcribe_by_VAD.py   # standalone VAD transcription helper
├── sb_code/
│   └── SendToIngest.cs      # Streamer.bot C# action code
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
| `httpx` | Async HTTP client (inter-service calls, OMDb, Giphy) |
| `python-dotenv` | `.env` loading |
