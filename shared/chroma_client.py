"""
shared/chroma_client.py

Singleton ChromaDB client shared across services.
Uses sentence-transformers for local embedding — no data leaves the box.

Usage:
    from shared.chroma_client import get_collection
    collection = get_collection()
    collection.add(documents=["..."], ids=["chunk_001"])
    results = collection.query(query_texts=["what did Twig say about disc golf?"], n_results=4)
"""

import chromadb
from chromadb.utils import embedding_functions
from shared.config import CHROMADB_DIR, CHROMA_COLLECTION, EMBEDDING_MODEL

_client: chromadb.ClientAPI | None = None
_collection = None


def get_client() -> chromadb.ClientAPI:
    global _client
    if _client is None:
        _client = chromadb.PersistentClient(path=str(CHROMADB_DIR))
    return _client


def get_collection():
    global _collection
    if _collection is None:
        client = get_client()
        ef = embedding_functions.SentenceTransformerEmbeddingFunction(
            model_name=EMBEDDING_MODEL
        )
        _collection = client.get_or_create_collection(
            name=CHROMA_COLLECTION,
            embedding_function=ef,
        )
    return _collection
