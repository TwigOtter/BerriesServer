# Implementation Notes

Changelog, roadmap, and decisions. Updated at the end of each work session.

---

## Roadmap

### Up Next
- [x] Register Discord app and complete bot invite (see checklist below)
- [ ] Configure Streamer.bot actions to POST all events to `ingest_api` with correct payloads

### Soon
- [ ] Per-user context injection — on `/event/mention`, pull the user's `users.db` record and append relevant fields (nickname, sub tier, etc.) to the system prompt
- [ ] `log: false` flag — when Streamer.bot sends `"log": false`, suppress JSONL + ChromaDB writes for semi-private exchanges
- [ ] Emote spam condensing — `PogChamp PogChamp PogChamp` → `PogChamp_x3` in `_preprocess_message()`

### Future
- [ ] StreamDeck organic conversation button — POST to `/event/mention` with `log: false, TTS: true, respond: true`; Berries reads recent context and joins the conversation naturally
- [ ] ChromaDB rebuild utility — script to reconstruct the index from `.jsonl` ground truth files
- [ ] TTS routing — when `TTS: true`, route Berries' response to SpeakerBot via a dedicated Streamer.bot action
- [ ] `berries_bot` as a standalone service — currently the response pipeline lives inside `ingest_api`; may be worth extracting once things stabilize

---

## Discord Setup Checklist

Bot code is complete. These manual steps remain:

- [x] Go to discord.com/developers/applications → New Application → name it "Berries"
- [x] Under **Bot**: create bot user, copy token → `.env` as `DISCORD_TOKEN`
- [x] Enable **Privileged Gateway Intents**: Server Members Intent + Message Content Intent
- [x] Under **OAuth2 → URL Generator**: scopes = `bot` + `applications.commands`
- [x] Permissions: Send Messages, Read Message History, View Channels, Use Slash Commands
- [x] Use the generated URL to invite Berries to the server
- [x] Get numeric channel IDs for Berries-designated channels → `.env` as `DISCORD_BERRIES_CHANNEL_IDS`
- [x] Test: `/ping` responds; Berries replies in configured channels
- [ ] Spec out additional Discord features before writing more code

---

## Decision Log

| Decision | Rationale |
|---|---|
| `stream_utils` service scrapped entirely | Streamer.bot handles first-time chatters, predictions, polls, and all Twitch events natively |
| `sentence-transformers` for embeddings | Free, runs locally, no data sent to external APIs |
| Streamer.bot owns "should Berries respond?" | Handles redeems, keyword triggers, subscriber gating — keeps that logic in the streaming tool |
| `/event/stream-update` not `/event/stream-start` | Streamer.bot fires its Update event on any title/category change mid-stream, not just at start |
| `/event/stream` uses pre-formatted `text` field | Streamer.bot has native string templating and direct access to event variables — no need to re-implement formatting in Python |
| `users.db` separate from transcript chunks | User profiles are relational and long-lived; chunks are append-only time-series — different access patterns |
| ChromaDB rebuilt from `.jsonl` if needed | Ground truth lives in flat files; ChromaDB is a derived index, never the source of truth |

---

## Changelog

### 2026-03-05

**personality.txt**
- Added explicit TECHNICAL rules prohibiting asterisk roleplay actions (`*narrows eyes*`), newlines, and markdown formatting — all of which break TTS readback

**`stream_utils/` — Removed entirely**
- Deleted `stream_utils/main.py`, `stream_utils/__init__.py`, `deploy/berries-utils.service`
- The SQLite first-time chatter detection was redundant; Streamer.bot has a native flag for this

**`shared/config.py`**
- Removed `SQLITE_DB_PATH`
- Added `USERS_DB_PATH = DATA_DIR / "users.db"`

**`shared/user_db.py` — New file**
- SQLite-backed user profile store
- Functions: `init_db()`, `upsert_user()`, `get_user()`, `set_nickname()`, `add_note()`, `increment_streams_watched()`
- Auto-creates `data/users.db` on first use

**`ingest_api/main.py`**
- Replaced deprecated `@app.on_event("startup")` with FastAPI lifespan context manager
- Added `_stream_metadata` module-level dict; updated `_flush_buffer()` to include `stream_title` and `stream_category` in every chunk
- Added `POST /event/stream-update` — caches title/category from Streamer.bot's Update event
- Added `POST /event/stream` — generic Twitch events (raids, subs, predictions, polls, etc.); Streamer.bot pre-formats the `text` field, server just buffers it
- Expanded `POST /event/chat` to accept `display_name`, `subscription_tier`, `subscription_months`, `gift_sub_count`; calls `upsert_user()` on every message
- Wired `_generate_response()`: now queries ChromaDB for long-term context and prepends `recent_chunks` deque for short-term context before calling LLM
- Removed `_format_stream_event()` switch case (logic moved to Streamer.bot)
- Startup now also calls `user_db.init_db()`

**`README.md` + `IMPLEMENTATION.md`**
- README trimmed to a clean architecture reference
- This file created to track changelog, roadmap, decisions, and setup checklists
