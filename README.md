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
     |── upserts ─────> data/movies.db                       (movie suggestions/history)
     |── responds ────> Streamer.bot webhook                 (Berries' replies)
     |── forwards ────> discord_bot :8002                    (going-live events)
     |
     | ask_berries_twitch()
     v
[shared/ask_berries.py]  ── LLM hub (nickname lookup, ChromaDB, prompt assembly, logging)
     |── reads ───────> data/chromadb/
     |── reads ───────> berries_bot/personality.txt
     |── calls ───────> LLM (Anthropic / Ollama)

[discord_bot]  ── discord.py, persistent WebSocket + FastAPI webhook :8002
     |── embeds ──────> data/chromadb/                       (watch channel messages)
     |── responds ────> Discord channel
     |
     | ask_berries_discord_mention()
     | ask_berries_movie_announcement()
     | ask_berries_twitch_going_live()
     v
[shared/ask_berries.py]
```

---

## Services

### `ingest_api` — The Front Door (port 8000)

Receives all events from Streamer.bot. Preprocesses, chunks, embeds, and responds.

| Endpoint | Trigger | Body |
|---|---|---|
| `POST /event/chat` | Every chat message | `userName`, `displayName`, `userId`, `msgId`, `message`, `messageStripped`, `emoteCount`, `role`, `bits`, `firstMessage`, `isSubscribed`, `subscriptionTier`, `monthsSubscribed`, `isVip`, `isModerator` |
| `POST /event/speech` | STT transcription | `speaker`, `text` |
| `POST /event/mention` | Berries response request | `text`, `username`, `CHAT` (bool), `TTS` (bool), `log` (bool) |
| `POST /event/stream-update` | Title/category change | `title`, `category` |
| `POST /event/stream` | Any other Twitch event | `type`, `text` (pre-formatted by Streamer.bot) |
| `POST /event/going-live` | Stream start | `title`, `category` — forwarded to discord_bot |
| `GET /health` | Status check | — |

All endpoints require `X-Secret: <INGEST_SECRET>` header.

**Flush conditions (buffer → JSONL + ChromaDB):**
- Token count ≥ `CHUNK_TOKEN_LIMIT` (default 480)
- `CHUNK_TIMEOUT_SEC` inactivity (default 5 min)

After each flush, the last `CHUNK_OVERLAP_SEC` seconds of entries (default 30s) are kept as the seed for the next chunk.

### `shared/ask_berries.py` — LLM Hub

All LLM calls go through here. Never called directly from non-service code.

| Function | Used by | Description |
|---|---|---|
| `ask_berries_twitch()` | `ingest_api` | Twitch @mention: nickname lookup, ChromaDB, prompt, LLM, log |
| `ask_berries_discord_mention()` | `discord_bot` | Discord @mention: same pipeline for Discord |
| `ask_berries_movie_announcement()` | `discord_bot` | Movie night: ChromaDB lookup + two-call announcement+gif pipeline |
| `ask_berries_twitch_going_live()` | `discord_bot` | Going-live: two-call announcement+gif pipeline |
| `ask_berries_discord()` | — | One-off in-character replies (available but currently unused) |
| `ask_berries()` | internal | Raw LLM call, no logging |

**Per-response context assembly (mention pipelines):**
1. Load `berries_bot/personality.txt` as system prompt base
2. Rewrite query → query ChromaDB for `CHROMA_N_RESULTS` semantically similar past chunks; discard any with L2 distance > `CHROMA_L2_THRESHOLD`
3. Inject last 2 flushed chunks from `recent_chunks` deque (Twitch short-term memory)
4. Call LLM → return response to service for delivery

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
| `data/huggingface/` | Local model cache for `nomic-embed-text-v1` |
| `data/documents/` | Drop `.md`/`.txt` files into `input/` to embed; processed files move to `archive/` |
| `data/users.db` | User profiles, passively built from chat events |
| `data/movies.db` | Movie suggestions and watch history |
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
| `ANTHROPIC_CHAT_MODEL` | `claude-sonnet-4-6` | Personality/chatbot calls (loads personality.txt) |
| `ANTHROPIC_ASSIST_MODEL` | `claude-haiku-4-5-20251001` | Utility tasks: query rewriting, gif queries |
| `OLLAMA_BASE_URL` | `http://localhost:11434` | Ollama endpoint |
| `OLLAMA_MODEL` | — | e.g. `llama3.1` |
| `INGEST_HOST` | `0.0.0.0` | ingest_api bind host |
| `INGEST_PORT` | `8000` | ingest_api bind port |
| `INGEST_SECRET` | — | Shared auth header between Streamer.bot and all services |
| `STREAMERBOT_CALLBACK_URL` | — | Where Berries POSTs his replies |
| `STREAMERBOT_RESPONSE_ACTION_ID` | — | Action ID to send back to Streamer.bot |
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
| `CHROMA_L2_THRESHOLD` | `0.8` | Discard ChromaDB results with L2 distance above this |
| `EMBEDDING_MODEL` | `nomic-ai/nomic-embed-text-v1` | Local sentence-transformers model |

---

## Directory Structure

```
BerriesServer/
├── berries_bot/
│   └── personality.txt      # Berries' system prompt (read by shared/ask_berries.py)
├── discord_bot/
│   └── main.py              # discord.py bot + FastAPI webhook (port 8002)
├── ingest_api/
│   └── main.py              # FastAPI event ingestion (port 8000)
├── shared/
│   ├── ask_berries.py       # LLM hub — all response pipelines
│   ├── call_logger.py       # structured LLM call logging
│   ├── chroma_client.py     # ChromaDB singleton + multi-query with L2 filtering
│   ├── config.py            # centralized config from .env
│   ├── llm_client.py        # Anthropic + Ollama abstraction + query rewriter
│   ├── movie_db.py          # movie suggestions/history SQLite CRUD
│   ├── prompt_builder.py    # system prompt assembly + context formatters
│   ├── tokenizer.py         # token counting (tiktoken)
│   └── user_db.py           # user profile SQLite CRUD
├── scripts/
│   ├── reindex_twitch.py    # rebuild ChromaDB from Twitch transcript JSONL files
│   ├── reindex_discord.py   # fetch and index Discord watch channel history
│   ├── embed_documents.py   # chunk and embed .md/.txt files from data/documents/input/
│   └── query_chroma.py      # interactive CLI for testing ChromaDB queries
├── data/
│   ├── transcripts/         # stream_chat_YYYY-MM-DD.jsonl files
│   ├── chromadb/            # ChromaDB persistence
│   ├── huggingface/         # local model cache (nomic-embed-text-v1)
│   ├── documents/           # input/ → embed_documents.py → archive/
│   ├── users.db             # auto-created on first run
│   └── movies.db            # auto-created on first run
├── deploy/
│   ├── berries-ingest.service
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
| `sentence-transformers` | Local embedding model (`nomic-ai/nomic-embed-text-v1`) |
| `einops` | Required by `nomic-embed-text-v1` |
| `anthropic` | Anthropic API client |
| `discord.py` | Discord bot |
| `tiktoken` | Token counting |
| `httpx` | Async HTTP client (inter-service calls, OMDb, Giphy) |
| `python-dotenv` | `.env` loading |
