"""
scripts/reindex_lore.py

Sync berries_bot/lore/*.md into ChromaDB as source:lore entries.

Each `## Heading` section of each lore file becomes one entry:
  id        lore_<file-stem>_<heading-slug>
  document  "<Heading>\n<section body>"
  metadata  {"source": "lore", "title": <Heading>, "file": <filename>}

The sync is full and idempotent: re-running upserts changed entries in place
and deletes ChromaDB lore entries that no longer exist in the files. Files
ending in `.example` are ignored.

Usage:
    python scripts/reindex_lore.py            # sync
    python scripts/reindex_lore.py --dry-run  # show what would change
"""

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from shared.config import BASE_DIR

LORE_DIR = BASE_DIR / "berries_bot" / "lore"


def _slugify(heading: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", heading.lower()).strip("-")
    return slug or "untitled"


def parse_lore_file(path: Path) -> list[dict]:
    """Split a lore markdown file into one entry per `## Heading` section."""
    entries: list[dict] = []
    heading: str | None = None
    body: list[str] = []

    def flush() -> None:
        if heading is None:
            return
        text = "\n".join(body).strip()
        if not text:
            return
        entries.append({
            "id": f"lore_{path.stem}_{_slugify(heading)}",
            "document": f"{heading}\n{text}",
            "metadata": {"source": "lore", "title": heading, "file": path.name},
        })

    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("## "):
            flush()
            heading = line[3:].strip()
            body = []
        elif heading is not None:
            body.append(line)
    flush()
    return entries


def collect_entries() -> list[dict]:
    entries: list[dict] = []
    if not LORE_DIR.exists():
        return entries
    for path in sorted(LORE_DIR.glob("*.md")):
        if path.name.lower() == "readme.md":
            continue
        file_entries = parse_lore_file(path)
        print(f"{path.name}: {len(file_entries)} entr{'y' if len(file_entries) == 1 else 'ies'}")
        entries.extend(file_entries)
    return entries


def main() -> None:
    parser = argparse.ArgumentParser(description="Sync lore files into ChromaDB.")
    parser.add_argument("--dry-run", action="store_true", help="show what would change without writing")
    args = parser.parse_args()

    entries = collect_entries()
    new_ids = {e["id"] for e in entries}
    if len(new_ids) != len(entries):
        sys.exit("Duplicate lore IDs — make sure headings are unique within each file.")

    from shared.chroma_client import get_collection
    collection = get_collection()

    existing = collection.get(where={"source": "lore"}, include=[])
    existing_ids = set(existing.get("ids", []))
    stale_ids = sorted(existing_ids - new_ids)

    print(f"\n{len(entries)} entries in files, {len(existing_ids)} in ChromaDB, {len(stale_ids)} stale")
    if args.dry_run:
        for e in entries:
            print(f"  upsert {e['id']}: {e['metadata']['title']}")
        for chunk_id in stale_ids:
            print(f"  delete {chunk_id}")
        print("\nDry run — nothing written.")
        return

    if entries:
        collection.upsert(
            ids=[e["id"] for e in entries],
            documents=[e["document"] for e in entries],
            metadatas=[e["metadata"] for e in entries],
        )
        print(f"Upserted {len(entries)} lore entries.")
    if stale_ids:
        collection.delete(ids=stale_ids)
        print(f"Deleted {len(stale_ids)} stale lore entries.")
    if not entries and not stale_ids:
        print("Nothing to do.")


if __name__ == "__main__":
    main()
