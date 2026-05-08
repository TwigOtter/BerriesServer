"""
chroma_diag3.py — isolate our embedding function from existing-collection state.

Three tests:
  A. Use our _NomicEmbeddingFunction with a FRESH collection (no inherited metadata)
  B. Add to existing collection by passing pre-computed embeddings directly
     (skips the Rust→Python embedding callback entirely)
  C. Upsert to existing collection by passing pre-computed embeddings directly

Run as berries:
    sudo -u berries venv/bin/python scripts/chroma_diag3.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import chromadb
from shared.config import CHROMADB_DIR, CHROMA_COLLECTION, DATA_DIR
from shared.chroma_client import _NomicEmbeddingFunction

print("[A1] Open client...")
client = chromadb.PersistentClient(path=str(CHROMADB_DIR))
print("    OK")

print("[A2] Build embedding function...")
ef = _NomicEmbeddingFunction(cache_folder=str(DATA_DIR / "huggingface"))
print("    OK")

print("[A3] Create FRESH 'diag3_nomic' WITH our embedding function...")
fresh = client.get_or_create_collection(name="diag3_nomic", embedding_function=ef)
print("    OK")

print("[A4] Add to fresh+nomic collection (lets ef compute embeddings)...")
fresh.add(
    ids=["d3a_001"],
    documents=["test document for diag3 fresh+nomic"],
    metadatas=[{"src": "diag3"}],
)
print("    OK")

print("[A5] Upsert to fresh+nomic collection...")
fresh.upsert(
    ids=["d3a_002"],
    documents=["test upsert document for diag3 fresh+nomic"],
    metadatas=[{"src": "diag3"}],
)
print("    OK")

print("[A6] Cleanup fresh collection...")
client.delete_collection("diag3_nomic")
print("    OK")

print("\n--- Existing-collection tests with PRE-COMPUTED embeddings ---")

print("[B1] Open existing collection (no embedding_function passed)...")
existing = client.get_collection(name=CHROMA_COLLECTION)
print(f"    OK: count={existing.count()}")

print("[B2] Pre-compute one embedding using our ef...")
embs = ef(["lord of the rings is twigs favorite"])
print(f"    OK: dim={len(embs[0])}")

print("[B3] Add to existing collection with pre-computed embedding...")
existing.add(
    ids=["__diag3_b_add"],
    documents=["lord of the rings is twigs favorite"],
    embeddings=embs,
    metadatas=[{"source": "diag3"}],
)
print("    OK")

print("[B4] Upsert to existing collection with pre-computed embedding...")
existing.upsert(
    ids=["__diag3_b_upsert"],
    documents=["lord of the rings is twigs favorite again"],
    embeddings=embs,
    metadatas=[{"source": "diag3"}],
)
print("    OK")

print("[B5] Cleanup our test rows from existing collection...")
existing.delete(ids=["__diag3_b_add", "__diag3_b_upsert"])
print("    OK")

print("\nAll diag3 steps passed.")
