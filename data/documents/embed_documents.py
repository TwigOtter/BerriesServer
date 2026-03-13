"""
data/documents/embed_documents.py

Chunks and embeds .md and .txt files from data/documents/input/ into ChromaDB,
then moves processed files to data/documents/archive/.

Re-run safe: existing ChromaDB entries for each document title are deleted before
re-inserting, so editing a file and re-dropping it into input/ always reflects
the latest content.

Usage (from project root with venv active):
    python data/documents/embed_documents.py [-t N] [-o N] [--dry-run]

    -t N / --token-limit N   Max tokens per chunk (default: 512)
    -o N / --overlap N       Overlap tokens carried into next chunk (default: 128)
    --dry-run                Chunk and print without writing to ChromaDB or archiving
"""

import argparse
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from shared.tokenizer import count_tokens
from shared.chroma_client import get_collection

SUPPORTED_EXTENSIONS = {".md", ".txt"}
INPUT_DIR = Path(__file__).parent / "input"
ARCHIVE_DIR = Path(__file__).parent / "archive"


def split_into_units(text: str, token_limit: int) -> list[str]:
    """
    Split text into line-level units, with word-level splitting for any single
    line that individually exceeds token_limit. Preserves blank lines so that
    document structure (markdown headers, paragraphs) stays intact.
    """
    units = []
    for line in text.splitlines():
        line = line.rstrip()
        if count_tokens(line) <= token_limit:
            units.append(line)
            continue
        # Line exceeds limit on its own — split word by word
        words = line.split()
        group: list[str] = []
        for word in words:
            group.append(word)
            if count_tokens(" ".join(group)) >= token_limit:
                if len(group) > 1:
                    units.append(" ".join(group[:-1]))
                    group = [group[-1]]
                else:
                    units.append(group[0])
                    group = []
        if group:
            units.append(" ".join(group))
    return units


def chunk_text(text: str, token_limit: int, overlap_tokens: int) -> list[str]:
    """
    Chunk text into token-bounded segments. Accumulates line units until adding
    the next unit would exceed token_limit, then flushes and carries forward the
    last overlap_tokens worth of lines into the next chunk.
    """
    units = split_into_units(text, token_limit)
    chunks: list[str] = []
    buffer: list[str] = []

    for unit in units:
        candidate = buffer + [unit]
        if count_tokens("\n".join(candidate)) >= token_limit and buffer:
            chunks.append("\n".join(buffer))
            # Trim buffer from the front until it fits within overlap_tokens
            while buffer and count_tokens("\n".join(buffer)) > overlap_tokens:
                buffer.pop(0)
            buffer.append(unit)
        else:
            buffer.append(unit)

    if buffer:
        chunks.append("\n".join(buffer))

    return chunks


def embed_file(
    path: Path,
    token_limit: int,
    overlap_tokens: int,
    collection,
    dry_run: bool,
) -> int:
    """
    Read, chunk, and embed a single file. Returns the number of chunks produced.
    """
    title = path.stem
    timestamp = datetime.now(timezone.utc).isoformat()
    text = path.read_text(encoding="utf-8")

    raw_chunks = chunk_text(text, token_limit, overlap_tokens)
    if not raw_chunks:
        print(f"  {path.name}: no content, skipping.")
        return 0

    chunk_dicts = [
        {
            "id": f"doc_{title}_{i:04d}",
            "text": chunk,
            "metadata": {
                "source": "document",
                "title": title,
                "date": timestamp,
                "token_count": count_tokens(chunk),
            },
        }
        for i, chunk in enumerate(raw_chunks)
    ]

    print(f"  {path.name}: {len(chunk_dicts)} chunk(s)")
    for c in chunk_dicts:
        preview = c["text"][:80].replace("\n", " ")
        print(f"    [{c['id']}] {c['metadata']['token_count']} tokens — {preview}...")

    if not dry_run:
        # Delete any existing chunks for this document before re-inserting
        existing = collection.get(where={"title": title})
        if existing["ids"]:
            collection.delete(ids=existing["ids"])
            print(f"    Deleted {len(existing['ids'])} existing chunk(s) for '{title}'.")

        collection.upsert(
            documents=[c["text"] for c in chunk_dicts],
            ids=[c["id"] for c in chunk_dicts],
            metadatas=[c["metadata"] for c in chunk_dicts],
        )
        print(f"    Upserted {len(chunk_dicts)} chunk(s) into ChromaDB.")

    return len(chunk_dicts)


def main():
    parser = argparse.ArgumentParser(description="Embed documents into ChromaDB.")
    parser.add_argument(
        "-t", "--token-limit",
        type=int,
        default=512,
        help="Max tokens per chunk (default: 512).",
    )
    parser.add_argument(
        "-o", "--overlap",
        type=int,
        default=128,
        help="Overlap tokens carried into the next chunk (default: 128).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Chunk and print without writing to ChromaDB or archiving files.",
    )
    args = parser.parse_args()

    INPUT_DIR.mkdir(parents=True, exist_ok=True)
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)

    files = sorted(f for f in INPUT_DIR.iterdir() if f.is_file() and f.suffix in SUPPORTED_EXTENSIONS)
    if not files:
        print("No supported files found in input/.")
        return

    label = " [DRY RUN]" if args.dry_run else ""
    print(f"Found {len(files)} file(s). token_limit={args.token_limit}, overlap={args.overlap}{label}\n")

    collection = None if args.dry_run else get_collection()
    total_chunks = 0

    for path in files:
        print(f"{path.name}:")
        n = embed_file(path, args.token_limit, args.overlap, collection, args.dry_run)
        total_chunks += n

        if not args.dry_run:
            dest = ARCHIVE_DIR / path.name
            shutil.move(str(path), str(dest))
            print(f"  Archived → archive/{path.name}")

        print()

    prefix = "[DRY RUN] " if args.dry_run else ""
    print(f"{prefix}Total: {total_chunks} chunk(s) from {len(files)} file(s).")


if __name__ == "__main__":
    main()
