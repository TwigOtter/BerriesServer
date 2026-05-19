# ChromaDB Collection Corruption — 2026-05-08

## Symptoms

- `dream.py` Phase 3 (RAG summarisation) consistently segfaulted in the ChromaDB upsert call.
- After much debugging, every fresh process attempting to *open* the `stream_transcripts` collection segfaulted — even with no upsert, no asyncio, and no embedding function passed.
- `discord_bot` and `ingest_api` appeared healthy because they were running with a collection handle cached in memory from a previous (healthy) startup. The moment `discord_bot` was restarted and an `@mention` triggered `get_collection()`, it crashed with `code=dumped, status=11/SEGV`.

## What We Ruled Out

In order, each of these was tested and eliminated as the cause:

1. **asyncio context** — segfault occurred in fresh subprocesses with no asyncio at all.
2. **Inherited threads** (httpx, tqdm, posthog, asyncio executor) — disabled one by one; segfault persisted.
3. **The Rust → Python embedding callback** — segfault occurred even when pre-computing embeddings and passing them directly.
4. **Multi-process write contention** — segfault persisted with `berries-ingest` and `berries-discord` stopped.
5. **Our `_NomicEmbeddingFunction` interface** — fresh collections using the same class succeeded; only the existing `stream_transcripts` collection crashed.
6. **`upsert()` specifically** — even `client.get_collection(name=...)` (read-only open, no embedding function) segfaulted.

## Root Cause

The on-disk `stream_transcripts` collection was corrupted. Specifically, the HNSW index files under `data/chromadb/3c231be4-…/` had not been written since **March 13**, while the SQLite metadata file (`chroma.sqlite3`) continued receiving writes through to today. Those two stores are supposed to stay in lockstep — when they desync, ChromaDB 1.5.1's Rust backend segfaults on read instead of raising a clean error.

Likely contributing factors, in descending order of likelihood:

1. **Previous `dream.py` segfaults mid-write.** Each crash happened in the upsert path with `os._exit(0)`-style termination (originally added to dodge torch teardown segfaults). That bypasses Python's commit-finalisation and may have left the SQLite write transaction in a partially-applied state. Each subsequent crashed run compounded the damage.
2. **Concurrent multi-process writes.** `ingest_api`, `discord_bot`, and `dream.py` all opened the same SQLite-backed `PersistentClient`. ChromaDB's PersistentClient is not officially designed for concurrent multi-process writes; lock conflicts can leave inconsistent state.
3. **Possible incomplete migration.** ChromaDB 1.5.1 was installed Feb 26. If the on-disk format from an earlier version wasn't fully migrated, that could have set up the desync.

## Phase 1 — Recovery

JSONL transcripts and document files in `data/transcripts/` and `data/documents/archive/` are the ground truth. ChromaDB is a derived index and can be rebuilt from them.

```bash
# 1. Stop services
sudo systemctl stop berries-ingest berries-discord

# 2. Preserve the broken db (in case forensics are needed later)
sudo -u berries cp -r /opt/berries/data/chromadb /opt/berries/data/chromadb.broken

# 3. Wipe the corrupted index
sudo -u berries rm -rf /opt/berries/data/chromadb

# 4. Rebuild from sources of truth
sudo -u berries venv/bin/python scripts/reindex_twitch.py
sudo -u berries venv/bin/python scripts/reindex_discord.py
sudo -u berries venv/bin/python scripts/embed_documents.py

# 5. Verify by running the deferred dream.py Phase 4 against the rebuilt collection
sudo -u berries venv/bin/python scripts/upsert_pending.py logs/daily_interactions/pending/2026-05-07_pending_summaries.json

# 6. Restart services
sudo systemctl start berries-ingest berries-discord
```

## Phase 2 — Prevent Recurrence

Migrate to ChromaDB's HTTP server architecture. A single `chroma run` server process holds the SQLite database open exclusively; `ingest_api`, `discord_bot`, `dream.py`, and reindex scripts all connect via HTTP. Removes the multi-process write hazard entirely.

Steps:

1. **Add `deploy/chroma-server.service`** — runs `chroma run --path /opt/berries/data/chromadb --host 127.0.0.1 --port 8001` as the `berries` user (drafted alongside this document).
2. **Update `shared/chroma_client.py`** — replace `chromadb.PersistentClient(path=…)` with `chromadb.HttpClient(host="127.0.0.1", port=8001)`. The embedding function logic stays the same, but a configurable `CHROMA_HOST` / `CHROMA_PORT` should land in `shared/config.py` and `.env`.
3. **Add ordering dependencies** — `berries-ingest.service`, `berries-discord.service`, and `berries-dream.service` should declare `Requires=chroma-server.service` and `After=chroma-server.service`.
4. **Backups** — extend `deploy/backup-dbs.sh` to snapshot the chromadb dir, and schedule it via systemd timer or cron. The current backups dir (`data/backups/`) had a chromadb snapshot from March 12 — old enough that the JSONL rebuild was the only reasonable option.

## Lessons

- **`os._exit(0)` is dangerous after writes.** It bypasses Python teardown including SQLite cleanup paths. Where we used it to dodge torch teardown segfaults, we may have caused the corruption we just recovered from. If we keep it, the upsert needs to happen well before the exit, and we should not re-introduce it in any process that does writes.
- **SQLite-backed ChromaDB is a single-writer design.** Multiple processes writing to the same `chroma.sqlite3` is a recipe for corruption regardless of how careful each process is.
- **Cached collection handles can mask corruption for hours.** A long-running service can look perfectly healthy while every fresh process crashes — the running service is just holding a stale-but-valid handle from before the corruption. When investigating "scripts crash, services work", restart a service and see if it still works.
- **A segfault is sometimes ChromaDB's way of saying "your data is inconsistent".** The Rust backend's error handling does not always raise on bad state. If something segfaults reproducibly during a read-only operation, suspect on-disk corruption, not your code.
