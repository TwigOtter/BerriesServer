"""
chroma_diag2.py — second-stage diagnostic.

Avoids our custom embedding function entirely. Uses ChromaDB's default
embedding function to create and write to a fresh test collection.

If this crashes, chromadb 1.5.1 is broken on this machine regardless of
embedding function — the install itself is the problem.

If this works, the issue is specifically how our _NomicEmbeddingFunction
interacts with the existing collection's stored metadata.

Run as berries:
    sudo -u berries venv/bin/python scripts/chroma_diag2.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import chromadb
from shared.config import CHROMADB_DIR

print("[1] Open client...")
client = chromadb.PersistentClient(path=str(CHROMADB_DIR))
print("    OK")

print("[2] List collections...")
print(f"    OK: {[c.name for c in client.list_collections()]}")

print("[3] Create FRESH 'diag2_default_ef' with default embedding function...")
fresh = client.get_or_create_collection(name="diag2_default_ef")
print("    OK")

print("[4] Add to fresh collection (default ef)...")
fresh.add(
    ids=["d2_001"],
    documents=["test document for diag2"],
    metadatas=[{"src": "diag2"}],
)
print("    OK")

print("[5] Upsert to fresh collection (default ef)...")
fresh.upsert(
    ids=["d2_002"],
    documents=["test upsert document for diag2"],
    metadatas=[{"src": "diag2"}],
)
print("    OK")

print("[6] Query fresh collection...")
results = fresh.query(query_texts=["test"], n_results=2)
print(f"    OK: {len(results['ids'][0])} results")

print("[7] Cleanup...")
client.delete_collection("diag2_default_ef")
print("    OK")

print("\nAll diag2 steps passed.")
