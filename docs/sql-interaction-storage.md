# Design: SQL-backed interaction storage

**Status:** Proposed — approved by Twig for implementation on a fresh branch
(after PR #12 merges). This doc is the spec; the implementing session should
treat decisions here as settled unless marked as an open question.

## Motivation

Today the ground truth for everything Berries remembers is JSONL chunk files
(`data/transcripts/*.jsonl`) plus ChromaDB. This has three structural
problems:

1. **JSONL stores chunks, not events.** A chunk is a retrieval-time artifact —
   ~480 tokens of interleaved messages sized for embedding. Persisting chunks
   as ground truth means the chunking strategy is frozen forever; we can never
   re-chunk with a better strategy because the raw per-message data is gone.
2. **The data is unqueryable.** "Recent interactions with this user," "recent
   channel activity," "last time someone mentioned X" — none of these are
   answerable from chunk files. This is also why Twig can't easily eyeball
   chat history.
3. **Response generation depends on fragile in-memory state.** `/event/mention`
   needs the `recent_chunks` deque and the live buffer text from ingest_api's
   process memory because there is nowhere else to get recency context from.
   A restart loses it. Discord watch chunks are worse: ChromaDB is their
   *only* home (no ground-truth file at all).

## Target architecture

**SQL becomes the system of record. ChromaDB becomes a fully derived index**,
built from SQL by a chunker job that can be rerun with any chunking strategy.

```
Streamer.bot ─→ ingest_api ──→ SQLite (twitch_events) ──┐
Discord      ─→ discord_bot ─→ SQLite (discord_messages)│
                                                        ├─→ chunker ─→ ChromaDB
Response path (stateless):                              │
  "user X on platform Y said Z"                         │
        ├── structured context: SQL queries ←───────────┘
        └── semantic context:   ChromaDB retrieval (shared/retrieval.py)
```

Two retrieval paths over one source of truth:

- **Structured (SQL):** recency, per-user history, per-channel history,
  keyword/BM25 search via FTS5. Strong where embeddings are weak (usernames,
  exact terms, time windows).
- **Semantic (ChromaDB):** "have we talked about something like this" — the
  existing rewrite → vector search → rerank pipeline in `shared/retrieval.py`
  stays as-is.

Both paths also map onto agent tools (`shared/tools.py`) for the experimental
tool-use loop: `get_recent_user_messages`, `search_messages`,
`get_channel_context`.

## Schemas

One SQLite database (suggested: `data/interactions.db`), **WAL mode + busy
timeout required** — ingest_api and discord_bot both write. Follow the
existing `shared/user_db.py` conventions (module of functions, `_connect()`,
`init_db()`, migrations).

```sql
CREATE TABLE twitch_events (
    id              INTEGER PRIMARY KEY,
    created_at      TEXT NOT NULL,      -- UTC ISO instant
    stream_date     TEXT NOT NULL,      -- local calendar day (LOCAL_TIMEZONE, see shared/config.py)
    stream_title    TEXT,
    stream_category TEXT,
    user_id         INTEGER,            -- Twitch numeric ID; NULL for system events
    username        TEXT,               -- login at time of event
    display_name    TEXT,
    type            TEXT NOT NULL,      -- 'message' | 'speech' | 'resub' | 'raid' | 'redeem' | 'stream_event' | ...
    content         TEXT,               -- message text / human-readable event text
    payload         TEXT,               -- JSON: bits, tier, months, raid size, emote data...
    message_id      TEXT UNIQUE,          -- Twitch message ID (for chat messages)
    reply_to_message_id TEXT,               -- Twitch message ID of the message being replied to
    is_bot          INTEGER NOT NULL DEFAULT 0,   -- includes Berries himself
    invoked_berries INTEGER NOT NULL DEFAULT 0,
);
CREATE INDEX idx_twitch_user_time ON twitch_events(user_id, created_at);
CREATE INDEX idx_twitch_time      ON twitch_events(created_at);

CREATE TABLE discord_messages (
    id                  INTEGER PRIMARY KEY,
    created_at          TEXT NOT NULL,  -- UTC ISO instant
    guild_id            TEXT,
    channel_id          TEXT NOT NULL,
    channel_name        TEXT,
    user_id             TEXT NOT NULL,  -- Discord snowflake
    username            TEXT,
    display_name        TEXT,
    message_id          TEXT UNIQUE,
    message_text        TEXT,
    reply_to_message_id TEXT,
    is_bot              INTEGER NOT NULL DEFAULT 0,   -- includes Berries himself
    invoked_berries     INTEGER NOT NULL DEFAULT 0,
);
CREATE INDEX idx_discord_user_time    ON discord_messages(user_id, created_at);
CREATE INDEX idx_discord_channel_time ON discord_messages(channel_id, created_at);
```

Plus FTS5 virtual tables over `content` / `message_text` (external-content
tables kept in sync with triggers), giving BM25 keyword search — this is the
"hybrid search" half of the RAG roadmap.

Design notes (settled in discussion):

- `payload` JSON column instead of packing resub/raid values into `content`:
  keeps `content` clean for text search while structured values stay queryable.
- `created_at` is a UTC instant; `stream_date` is a local calendar day —
  consistent with the LOCAL_TIMEZONE convention established for the daily
  logs (absolute instants UTC, calendar-day labels local).
- Speech-to-text transcription rows use `type = 'speech'` in `twitch_events`.

## Stateless response generation

With SQL in place, `/event/mention` no longer needs process-local state:

- `recent_chunks` deque → `SELECT ... ORDER BY created_at DESC LIMIT n` over
  `twitch_events`.
- `recent_buffer_text` (query-rewriter context) → same query, smaller window.
- Discord channel history (currently fetched live from the Discord API in
  `cogs/mention.py`) → can come from `discord_messages`, though the live API
  fetch also works; implementer's choice.

This slots into the existing `ContextProvider` seam (`shared/context_providers.py`):
`RecentChunksProvider` becomes a `RecentEventsProvider` that queries SQL.
The end state is the invocation Twig described: an endpoint that means
"this user on this platform asked this question" with no dependency on
what's in ingest_api's memory. Restart-safe.

The only remaining in-memory structure is the chunk-assembly buffer, and even
that can be eliminated if the chunker becomes a periodic job reading SQL
(Phase 3).

## Migration phases

Each phase is independently shippable. Do not big-bang this.

1. **Tables + dual-write.** New `shared/interactions_db.py` with the schemas
   above. ingest_api (`/event/chat`, `/event/speech`, `/event/stream`,
   `/event/mention`) and the Discord watcher/mention cogs write to SQL *in
   addition to* the existing JSONL/Chroma flow. Zero behavioral change.
   Berries' own Discord replies should be recorded too (`is_bot = 1`).
2. **Stateless context.** Replace `recent_chunks`/`recent_buffer_text` in the
   mention pipelines with SQL queries via a new context provider. Remove the
   deque. ingest_api restart no longer loses response context.
3. **Derived chunker.** A job (script or background task) builds ChromaDB
   chunks from SQL rows — same ~480-token sizing initially, but now
   re-runnable. Chunk IDs should be derivable from row ID ranges so re-runs
   are idempotent. Retire JSONL writes. Existing Chroma chunks stay untouched
   (no retrieval gap); old JSONL files are kept as archive.
4. **FTS5 + agent tools.** Keyword search tables/triggers, plus SQL-backed
   tools in `shared/tools.py` for the tool-use loop.
5. **(Later, optional)** Migrate `interaction_log`/`retrieval_log` daily JSON
   files into tables; dreaming queries SQL directly.

## Historical data

Parsing old JSONL chunks back into per-message rows is possible (`[Name]: text`
lines are regular) but lossy on metadata (no user IDs, no per-message
timestamps — only chunk-level ranges). Decision: **start fresh in SQL**; the
old Chroma index keeps serving long-term memory, so nothing is lost at
retrieval time. A best-effort backfill script is optional polish, not part of
the migration.

## Out of scope

- Replacing ChromaDB / vector retrieval (SQL complements it, doesn't replace it).
- Changing the dreaming pipeline (beyond Phase 5, later).
- Postgres or any external DB server — SQLite matches the operational
  footprint of `users.db`/`movies.db` and handles this write volume trivially.

## Open questions for implementation

- One DB file (`interactions.db`) vs. folding into an existing one: one new
  file is suggested, keeping `users.db` (profiles) separate from event firehose.
  - Yes, one new file -- `interactions.db` is the plan.
- Retention: tables grow forever; fine for now, but a pruning/archival story
  may eventually be wanted for `discord_messages` in busy channels.
  - We aren't that busy, we can keep it all for now.
- Whether Discord mention channel-history should switch to SQL or stay on the
  live API fetch (SQL only covers watched channels; the live fetch covers any
  channel Berries is mentioned in — probably keep the live fetch as fallback).
  - Live fetch should be the primary source, and SQL can be used for long term history and for watched channels.
