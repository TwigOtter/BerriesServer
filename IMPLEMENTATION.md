# Implementation Notes

Changelog, roadmap, and decisions. Updated at the end of each work session.

---

## Roadmap

### Up Next
- [x] Register Discord app and complete bot invite (see checklist below)
- [x] Configure Streamer.bot actions to POST all events to `ingest_api` with correct payloads

### Soon
- [x] Restructure movie commands as a Discord subcommand group — `/movie suggest add`, `/movie suggest remove`, `/movie suggest list`, `/movie announce` (replaces `/movie-time`; fires LLM announcement + role ping + Giphy GIF + marks watched), `/movie history list`, `/movie history remove` (mod-only). `/movie announce` sits as a top-level subcommand alongside the `suggest` and `history` subcommand groups, which is valid in Discord's API.
- [x] `/movie suggest add` disambiguation — OMDb search returns multiple results; instead of picking the first match, present the user with a numbered list of up to 5 candidates and prompt them to reply with 1–5 (or "cancel"). Also consider a standalone `/movie search <query>` command that returns candidate titles with their IMDB links so users can confirm the right film before suggesting it. — OMDb search returns multiple results; instead of picking the first match, present the user with a numbered list of up to 5 candidates and prompt them to reply with 1–5 (or "cancel"). Also consider a standalone `/search-movies <query>` command that returns candidate titles with their IMDB links so users can confirm the right film before suggesting it.
- [x] Add logging to `discord_bot` — file-based rotating log to `logs/discord_bot.log` is implemented; detailed per-command logging and GIF failure tracking still missing
- [ ] Per-user context injection — on `/event/mention`, pull the user's `users.db` record and append relevant fields (nickname, sub tier, etc.) to the system prompt
- [ ] `log: false` flag — when Streamer.bot sends `"log": false`, suppress JSONL + ChromaDB writes for semi-private exchanges
- [x] Emote spam condensing — `PogChamp PogChamp PogChamp` → `PogChamp x3` in `_preprocess_message()` (uses `messageStripped` to identify emote tokens)

- [ ] **Twitch↔Discord account linking** — Discord side is done: `/twitch-link <twitch_username>` slash command and `link_discord()` in `user_db.py` are implemented. Remaining: Streamer.bot Warn API verification step — generate a short code, trigger a Twitch Warn (via Streamer.bot) with "use this code in Discord: XXXX", user confirms with `/verify <code>`. Note: Warn API requires the bot account to have moderator privileges and may create a moderation record — verify whether Twitch logs warns in a way that's visible to the user or to Twitch itself before shipping. Once fully linked, users can manage their profile via Discord commands (`/set-nickname`, `/set-timezone`, etc.).
- [ ] **User timezone** — store `timezone` (IANA string, e.g. `America/Chicago`) per user in `users.db`. `/set-timezone <city or zone>` to register. `/time [user] [time]` converts a natural-language timestamp (parsed with `dateparser`) from the invoker's timezone to the target user's timezone; returns an error if either user has no timezone set. Need to spec accepted input format — `dateparser` handles `"3pm"`, `"15:00"`, `"now"`, etc.
- [ ] **`/temp <value> <unit> to <unit>`** — convert between F, C, and K; pure math, no external deps.

### Future
- [ ] StreamDeck organic conversation button — POST to `/event/mention` with `log: false, TTS: true, respond: true`; Berries reads recent context and joins the conversation naturally
- [ ] ChromaDB rebuild utility — script to reconstruct the index from `.jsonl` ground truth files
- [ ] TTS routing — when `TTS: true`, route Berries' response to SpeakerBot via a dedicated Streamer.bot action
- [ ] **`berries_bot/main.py` is dead code** — the entire response pipeline (LLM call, ChromaDB context, Streamer.bot callback with `CHAT`/`TTS`/`action_id`) now lives in `ingest_api`. `berries_bot`'s `/respond` endpoint and `post_to_twitch()` are never called; the service runs but does nothing. Options: delete `berries_bot/main.py` and the `berries-bot.service` systemd unit, or gut it and repurpose it as a true standalone if separation is ever desired.
- [x] **`embed_docs.py` — manual document embedding script** — script is at `data/documents/embed_documents.py`; reads `.txt`/`.md` files from `data/documents/`, chunks each file, and embeds into ChromaDB with `{"source": "document"}` metadata. Designed to run on demand. Use case: lore, story writing, reference docs Berries should be able to draw from.
- [x] **Discord message embedding** — implemented as "Discord channel monitoring" (see item directly below); Discord watch channel messages are embedded into ChromaDB with `{"source": "discord"}` metadata via `DISCORD_WATCH_CHANNEL_IDS`.
- [x] **Discord channel monitoring (RAG source)** — allow Berries to silently watch specific Discord channels (configured via `DISCORD_WATCH_CHANNEL_IDS` env var) and embed their messages into ChromaDB for RAG context, without responding. Separate from `DISCORD_BERRIES_CHANNEL_WHITELIST_IDS` (channels that skip the 2-message redirect check). Enables community lore, inside jokes, and channel history to inform his responses.
- [ ] **Discord mention rate limiting** — prevent @mention spam by adding a per-user cooldown (e.g. 60s) and/or a per-channel cooldown in `discord_bot`. When rate-limited, either silently ignore or send a short in-character brush-off. Cooldown values configurable via `.env`.

---

## Discord Setup Checklist

Bot code is complete. These manual steps remain:

- [x] Go to discord.com/developers/applications → New Application → name it "Berries"
- [x] Under **Bot**: create bot user, copy token → `.env` as `DISCORD_TOKEN`
- [x] Enable **Privileged Gateway Intents**: Server Members Intent + Message Content Intent
- [x] Under **OAuth2 → URL Generator**: scopes = `bot` + `applications.commands`
- [x] Permissions: Send Messages, Read Message History, View Channels, Use Slash Commands
- [x] Use the generated URL to invite Berries to the server
- [x] Get numeric channel IDs for whitelist channels → `.env` as `DISCORD_BERRIES_CHANNEL_WHITELIST_IDS`
- [x] Test: `/ping` responds; Berries replies in configured channels
- [x] Spec out additional Discord features before writing more code
- [x] Get OMDb API key (free at omdbapi.com) → `.env` as `OMDB_API_KEY`
- [x] Get Giphy API key (free at developers.giphy.com) → `.env` as `GIPHY_API_KEY`
- [x] Set `DISCORD_ANNOUNCE_CHANNEL_ID` — channel for going-live and movie night announcements
- [x] Set `DISCORD_EVENT_ROLE_ID` — right-click role → Copy Role ID (requires Developer Mode)
- [x] Set `DISCORD_STREAM_ROLE_ID` — same as above
- [ ] Configure Streamer.bot to `POST /event/going-live` with `{"title": "...", "category": "..."}` on stream start

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
| ChromaDB metadata is additive, not enforced | ChromaDB is schema-less — new fields can be added to new documents freely without touching old ones. Old stream chunks won't have `source` set; that's fine for unfiltered queries. Only add a backfill step if you specifically need to filter on a field across all historical data. Rule: add fields freely, never rename or remove them. |
| `source` metadata field planned but not yet on stream chunks | Current stream chunks don't include `source`; future document and Discord embeds will use `{"source": "document"}` / `{"source": "discord"}`. Stream chunks can be backfilled via the rebuild utility when needed — no urgency since unfiltered queries still work correctly. |

---

## Changelog

### 2026-03-09

**`discord_bot/main.py` — `/movie suggest add` disambiguation**
- Added `_omdb_search_many(title, limit=5)` helper; `_omdb_search` now delegates to it
- Added `MovieSelectView` — ephemeral `discord.ui.View` with a select menu; only the invoking user sees or can interact with it; 60 s timeout
- When OMDb returns multiple results, bot sends an ephemeral dropdown (up to 5 options + Cancel); user picks from the menu, no channel messages involved
- Single-result searches skip the dropdown entirely
- All error/cancel/timeout responses are ephemeral; only the final success posts publicly to the channel

**`discord_bot/main.py` — Movie commands restructured as `/movie` subcommand group**
- Replaced flat slash commands with a `movie` `app_commands.Group` containing `suggest` and `history` subgroups
- `/suggest-movie` → `/movie suggest add`
- `/suggested-movies` → `/movie suggest list`
- `/remove-suggestion` → `/movie suggest remove` (mod-only)
- `/movie-time` → `/movie announce` (mod-only)
- `/past-movies` → `/movie history list`
- `/movie history remove` — new mod-only command; removes a movie from watch history

**`shared/movie_db.py`**
- Added `remove_watched(imdb_id)` — deletes a watched movie from history; used by `/movie history remove`

**Roadmap updates**
- Marked "Restructure movie commands" as done

---

### 2026-03-08

**`ingest_api/main.py` — `/event/chat` schema overhaul**
- Replaced old snake_case payload fields with Streamer.bot's native camelCase variable names: `userName`, `displayName`, `userId`, `msgId`, `message`, `messageStripped`, `emoteCount`, `role`, `bits`, `firstMessage`, `isSubscribed`, `subscriptionTier`, `monthsSubscribed`, `isVip`, `isModerator`
- `role` replaces `is_moderator`; mapped via `_ROLE_LABELS` dict (1=Viewer, 2=VIP, 3=Moderator, 4=Broadcaster)
- `subscriptionTier` uses Streamer.bot's 1000/2000/3000 encoding — mapped to internal 1/2/3 on ingest
- Restored sub tracking (`subscription_tier`, `subscription_months`) passed to `upsert_user()`
- All parsed int fields now go through `_safe_int()` helper, which handles empty strings and unsubstituted `%variable%` placeholders without crashing
- Log line updated to include role, sub tier/months, VIP, mod, first-message, and bits flags

**`ingest_api/main.py` — `_preprocess_message()` — emote-aware condensing**
- Added `text_stripped` and `emote_count` parameters
- When `emoteCount > 0`, diffs the original message against `messageStripped` to identify emote tokens; condenses consecutive identical emote runs: `"PogChamp PogChamp PogChamp"` → `"PogChamp x3"`
- Resolves the longstanding TODO for emote spam condensing

**`ingest_api/main.py` — `_safe_int()` helper added**
- Guards all int parsing against missing, empty, or unsubstituted Streamer.bot template values

**`sb_code/SendToIngest.cs` — New file**
- C# Streamer.bot inline action that POSTs any `ingestString` JSON payload to `requestUrl`
- Reads `Berries_IngestSecret` and `Berries_IngestUrl` from Streamer.bot persisted globals
- Adds `X-Secret` header when secret is set; logs errors cleanly without crashing the action

**`documentation/streamerbot-setup-checklist.md` — New file**
- Setup checklist covering all 6 ingest endpoints with payload templates, suggested triggers, and per-action checkboxes
- All actions confirmed working in testing (all boxes checked)

**`berries_bot/personality.txt`**
- Added pronouns: Berries uses they/them; instruct the LLM to correct gendered pronouns in character with eerie amusement

**Roadmap updates**
- Marked "Emote spam condensing" as done
- Marked "Configure Streamer.bot actions" as done

---

### 2026-03-06

#### DONE: Fix git push credentials

The repo remote is now set to SSH (`git@github.com:TwigOtter/BerriesServer.git`) but
`core.sshCommand` is hardcoded to `/opt/berries_ssh/id_ed25519` (a read-only deploy key
owned by `berries`). `twig` can't read it and `berries` can't write with it.

**Fix (do when back at a real machine):**

1. Add `twig`'s personal public key to GitHub as a **user SSH key** (not a deploy key):
   ```
   cat ~/.ssh/id_ed25519.pub
   ```
   Go to: GitHub → Settings → SSH and GPG keys → New SSH key → paste it in.

2. Remove the hardcoded SSH key override from the repo config:
   ```
   git config --unset core.sshCommand
   ```

After that, `git push` as `twig` will work without any prompts.

---

### 2026-03-05

**`discord_bot/main.py` — Major rewrite**

- **Bug fix:** Added `await bot.tree.sync()` to `on_ready` — slash commands were defined but never registered with Discord's API, so `/ping` wasn't appearing
- **@mention response:** Berries now responds when `@BerriesTheDemon` is mentioned in *any* channel (not just whitelisted ones); mention is stripped from content before LLM call
- **Entry point changed:** `bot.run()` → `asyncio.run(_main())` using `bot.start()` inside `asyncio.gather` so the FastAPI webhook server runs concurrently
- **Webhook server (port 8002):** New FastAPI app (`webhook_app`) runs alongside the bot; receives `POST /event/going-live` forwarded from `ingest_api`
- **Going-live announcements:** LLM generates in-character announcement → pings `@Stream Notifications` role → appends Twitch link → fetches Giphy GIF → posts to announce channel
- **New slash commands:**
  - `/suggest-movie <title>` — OMDb lookup for canonical title/IMDB ID; rejects with in-character LLM message if already suggested or watched within 365 days
  - `/suggested-movies` — lists current open suggestions
  - `/past-movies` — lists watch history with dates (capped at 20)
  - `/movie-time <title>` — mod-only (`manage_messages`); OMDb lookup → LLM announcement → pings `@Event Notifications` role → Giphy GIF → posts to announce channel → marks movie as watched in DB
- **Giphy GIF integration:** On announcements, a second LLM call generates a 2–5 word search query; Giphy returns 8 results; a random pick from the top 5 is appended as a URL (Discord auto-embeds)

**`shared/movie_db.py` — New file**
- SQLite movie store sharing `data/users.db`
- Tracks suggestions and watch history by IMDB ID (canonical key prevents duplicates regardless of title spelling)
- Functions: `init_movie_db()`, `add_suggestion()`, `get_suggestion()`, `get_all_suggestions()`, `get_recent_watched()`, `get_all_watched()`, `mark_watched()`

**`ingest_api/main.py`**
- Added `POST /event/going-live` — receives event from Streamer.bot (with shared secret auth), forwards to `discord_bot` webhook at `DISCORD_BOT_WEBHOOK_URL`

**`shared/config.py`**
- Added: `DISCORD_ANNOUNCE_CHANNEL_ID`, `DISCORD_BOT_WEBHOOK_PORT`, `DISCORD_BOT_WEBHOOK_URL`, `DISCORD_EVENT_ROLE_ID`, `DISCORD_STREAM_ROLE_ID`, `OMDB_API_KEY`, `GIPHY_API_KEY`

**Decision: Tenor → Giphy**
- Tenor API is being deprecated; switched to Giphy (same shape: search → URL)

**Decision: OMDb for movie canonicalization**
- Users type free-form titles; OMDb returns a canonical title + IMDB ID used as the DB key — prevents duplicates like "LOTR" vs "Lord of the Rings: Fellowship of the Ring"

**Decision: Manual polls**
- Two-stage Discord poll workflow (all suggestions → top 2 → winner) kept manual; automating poll creation and result-querying was out of scope

---

### 2026-03-04

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
