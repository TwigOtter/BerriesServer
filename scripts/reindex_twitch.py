"""
scripts/reindex_twitch.py

Rebuilds ChromaDB from Twitch transcript JSONL files (the ground truth).
Safe to run multiple times — uses upsert so existing entries are overwritten
with the same data rather than duplicated.

Usage (from project root with venv active):
    python scripts/reindex_twitch.py [--dry-run]
"""

import argparse
import json
import sys
from pathlib import Path

# Allow imports from project root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from shared.config import TRANSCRIPTS_DIR
from shared.chroma_client import get_collection


def main():
    parser = argparse.ArgumentParser(description="Re-index Twitch transcripts into ChromaDB.")
    parser.add_argument("--dry-run", action="store_true", help="Parse and count chunks without writing to ChromaDB.")
    args = parser.parse_args()

    jsonl_files = sorted(TRANSCRIPTS_DIR.glob("stream_chat_*.jsonl"))
    if not jsonl_files:
        print(f"No transcript files found in {TRANSCRIPTS_DIR}")
        sys.exit(1)

    print(f"Found {len(jsonl_files)} transcript file(s).")

    collection = None if args.dry_run else get_collection()

    total_chunks = 0
    total_errors = 0

    for jsonl_path in jsonl_files:
        file_chunks = 0
        with open(jsonl_path, encoding="utf-8") as f:
            for lineno, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    chunk = json.loads(line)
                except json.JSONDecodeError as e:
                    print(f"  SKIP {jsonl_path.name}:{lineno} — JSON error: {e}")
                    total_errors += 1
                    continue

                chunk_id = chunk.get("chunk_id")
                text = chunk.get("text")
                if not chunk_id or not text:
                    print(f"  SKIP {jsonl_path.name}:{lineno} — missing chunk_id or text")
                    total_errors += 1
                    continue

                if not args.dry_run:
                    collection.upsert(
                        documents=[text],
                        ids=[chunk_id],
                        metadatas=[{
                            "stream_date": chunk.get("stream_date", ""),
                            "stream_title": chunk.get("stream_title", ""),
                            "stream_category": chunk.get("stream_category", ""),
                            "start_time": chunk.get("start_time", ""),
                            "end_time": chunk.get("end_time", ""),
                            "flush_reason": chunk.get("flush_reason", ""),
                            "token_count": chunk.get("token_count", 0),
                        }],
                    )
                file_chunks += 1

        print(f"  {jsonl_path.name}: {file_chunks} chunk(s)")
        total_chunks += file_chunks

    print(f"\n{'[DRY RUN] Would upsert' if args.dry_run else 'Upserted'} {total_chunks} chunk(s) total.")
    if total_errors:
        print(f"Skipped {total_errors} malformed line(s).")


if __name__ == "__main__":
    main()
