"""
scripts/reindex_discord.py

Fetches recent message history from Discord watch channels and indexes them
into ChromaDB using the same chunking logic as the live bot.

Chunk IDs are deterministic (based on channel ID + first message ID in the
chunk), so this is safe to re-run — existing chunks are upserted, not duplicated.

Usage (from project root with venv active):
    python scripts/reindex_discord.py [--limit N] [--dry-run]

    --limit N    Messages to fetch per channel (default: 1000, Discord max per run: no hard limit)
    --dry-run    Fetch and chunk without writing to ChromaDB
"""

import argparse
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from shared.config import DISCORD_TOKEN, DISCORD_WATCH_CHANNEL_IDS, CHUNK_TOKEN_LIMIT, DISCORD_CHUNK_OVERLAP_MESSAGES
from shared.tokenizer import count_tokens
from shared.chroma_client import get_collection

DISCORD_API = "https://discord.com/api/v10"


def fetch_channel_messages(token: str, channel_id: int, limit: int) -> list[dict]:
    """Fetch up to `limit` messages from a channel, oldest-first."""
    headers = {"Authorization": f"Bot {token}"}
    messages = []
    before = None

    with httpx.Client(headers=headers, timeout=30.0) as client:
        while len(messages) < limit:
            batch_size = min(100, limit - len(messages))
            params = {"limit": batch_size}
            if before:
                params["before"] = before

            resp = client.get(f"{DISCORD_API}/channels/{channel_id}/messages", params=params)
            if resp.status_code == 429:
                retry_after = resp.json().get("retry_after", 1.0)
                print(f"    Rate limited — waiting {retry_after}s")
                time.sleep(retry_after)
                continue
            resp.raise_for_status()

            batch = resp.json()
            if not batch:
                break

            messages.extend(batch)
            before = batch[-1]["id"]  # oldest in this batch; paginate further back

            if len(batch) < batch_size:
                break  # no more messages available

    # Discord returns newest-first; reverse to chronological order
    messages.reverse()
    return messages


def chunk_messages(messages: list[dict], channel_id: int, channel_name: str, guild_id: str) -> list[dict]:
    """
    Chunk messages using the same token-limit logic as _flush_watch_channel.
    Returns a list of chunk dicts ready to upsert into ChromaDB.
    """
    chunks = []
    buffer: list[dict] = []

    def flush(buf: list[dict]) -> dict | None:
        if not buf:
            return None
        text = "\n".join(e["text"] for e in buf)
        first_msg_id = buf[0]["id"]
        chunk_id = f"discord_reindex_{channel_id}_{first_msg_id}"
        return {
            "id": chunk_id,
            "text": text,
            "metadata": {
                "source": "discord",
                "channel_id": str(channel_id),
                "channel_name": channel_name,
                "guild_id": guild_id,
                "start_time": buf[0]["timestamp"],
                "end_time": buf[-1]["timestamp"],
                "flush_reason": "reindex",
                "token_count": count_tokens(text),
            },
        }

    for msg in messages:
        content = msg.get("content", "").strip()
        if not content:
            continue
        author = msg.get("author", {}).get("global_name") or msg.get("author", {}).get("username", "Unknown")
        entry = {
            "id": msg["id"],
            "text": f"[{author}]: {content}",
            "timestamp": msg["timestamp"],
        }
        buffer.append(entry)

        buf_text = "\n".join(e["text"] for e in buffer)
        if count_tokens(buf_text) >= CHUNK_TOKEN_LIMIT:
            chunk = flush(buffer)
            if chunk:
                chunks.append(chunk)
            # Keep overlap (same as live bot)
            overlap = buffer[-DISCORD_CHUNK_OVERLAP_MESSAGES:]
            while len(overlap) > 1 and count_tokens("\n".join(e["text"] for e in overlap)) >= CHUNK_TOKEN_LIMIT:
                overlap = overlap[1:]
            buffer = overlap

    # Flush remaining messages as final chunk
    chunk = flush(buffer)
    if chunk:
        chunks.append(chunk)

    return chunks


def main():
    parser = argparse.ArgumentParser(description="Re-index Discord watch channels into ChromaDB.")
    parser.add_argument("--limit", type=int, default=1000, help="Messages to fetch per channel (default: 1000).")
    parser.add_argument("--dry-run", action="store_true", help="Fetch and chunk without writing to ChromaDB.")
    args = parser.parse_args()

    if not DISCORD_TOKEN:
        print("ERROR: DISCORD_TOKEN not set in .env")
        sys.exit(1)
    if not DISCORD_WATCH_CHANNEL_IDS:
        print("ERROR: DISCORD_WATCH_CHANNEL_IDS not set in .env")
        sys.exit(1)

    collection = None if args.dry_run else get_collection()
    headers = {"Authorization": f"Bot {DISCORD_TOKEN}"}

    total_chunks = 0

    for channel_id in DISCORD_WATCH_CHANNEL_IDS:
        print(f"\nChannel {channel_id}:")

        # Resolve channel name and guild ID
        with httpx.Client(headers=headers, timeout=10.0) as client:
            resp = client.get(f"{DISCORD_API}/channels/{channel_id}")
            resp.raise_for_status()
            channel_info = resp.json()
        channel_name = channel_info.get("name", str(channel_id))
        guild_id = str(channel_info.get("guild_id", ""))
        print(f"  #{channel_name} (guild {guild_id})")

        print(f"  Fetching up to {args.limit} messages...")
        messages = fetch_channel_messages(DISCORD_TOKEN, channel_id, args.limit)
        print(f"  Fetched {len(messages)} message(s).")

        chunks = chunk_messages(messages, channel_id, channel_name, guild_id)
        print(f"  Produced {len(chunks)} chunk(s).")

        if not args.dry_run and chunks:
            collection.upsert(
                documents=[c["text"] for c in chunks],
                ids=[c["id"] for c in chunks],
                metadatas=[c["metadata"] for c in chunks],
            )
            print(f"  Upserted {len(chunks)} chunk(s) into ChromaDB.")

        total_chunks += len(chunks)

    print(f"\n{'[DRY RUN] ' if args.dry_run else ''}Total: {total_chunks} chunk(s) across {len(DISCORD_WATCH_CHANNEL_IDS)} channel(s).")


if __name__ == "__main__":
    main()
