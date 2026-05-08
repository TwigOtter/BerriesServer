"""
scripts/chroma_diag.py

Minimal ChromaDB diagnostic — narrow down where the segfault lives.

Run as the `berries` user:
    sudo -u berries venv/bin/python scripts/chroma_diag.py

Tests, in order:
  1. Open the existing client (no writes)
  2. Read existing collection's count
  3. Add (not upsert) one row to the EXISTING collection
  4. Create a SEPARATE fresh collection
  5. Add one row to the FRESH collection
  6. Upsert one row to the FRESH collection

Whichever step crashes tells us where the bug is.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import chromadb
from shared.config import CHROMADB_DIR, CHROMA_COLLECTION
from shared.chroma_client import _NomicEmbeddingFunction, get_client
from shared.config import DATA_DIR

print("[1] Opening client...")
client = get_client()
print("    OK")

print("[2] Listing collections...")
cols = client.list_collections()
print(f"    OK, {len(cols)} collection(s): {[c.name for c in cols]}")

ef = _NomicEmbeddingFunction(cache_folder=str(DATA_DIR / "huggingface"))
print("[3] Getting existing collection...")
existing = client.get_or_create_collection(name=CHROMA_COLLECTION, embedding_function=ef)
print(f"    OK, existing.count() = {existing.count()}")

print("[4] add() one row to EXISTING collection...")
existing.add(
    ids=["__diag_add_existing_001"],
    documents=["diagnostic add to existing collection"],
    metadatas=[{"source": "diag"}],
)
print("    OK")
existing.delete(ids=["__diag_add_existing_001"])

print("[5] upsert() one row to EXISTING collection...")
existing.upsert(
    ids=["__diag_upsert_existing_001"],
    documents=["diagnostic upsert to existing collection"],
    metadatas=[{"source": "diag"}],
)
print("    OK")
existing.delete(ids=["__diag_upsert_existing_001"])

print("[6] Creating FRESH collection 'chroma_diag_test'...")
fresh = client.get_or_create_collection(name="chroma_diag_test", embedding_function=ef)
print("    OK")

print("[7] add() one row to FRESH collection...")
fresh.add(
    ids=["diag_001"],
    documents=["fresh collection add test"],
    metadatas=[{"source": "diag"}],
)
print("    OK")

print("[8] upsert() one row to FRESH collection...")
fresh.upsert(
    ids=["diag_002"],
    documents=["fresh collection upsert test"],
    metadatas=[{"source": "diag"}],
)
print("    OK")

print("[9] Cleanup: deleting fresh collection...")
client.delete_collection(name="chroma_diag_test")
print("    OK")

print("\nAll steps passed.")
