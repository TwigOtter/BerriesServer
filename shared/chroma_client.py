"""
shared/chroma_client.py

Singleton ChromaDB client shared across services.
Uses nomic-embed-text-v1 for local embedding — no data leaves the box.

Usage:
    from shared.chroma_client import get_collection
    collection = get_collection()
    collection.add(documents=["..."], ids=["chunk_001"])
    results = collection.query(query_texts=["what did Twig say about disc golf?"], n_results=4)
"""

import chromadb
from shared.config import CHROMADB_DIR, CHROMA_COLLECTION, DATA_DIR

_client: chromadb.ClientAPI | None = None
_collection = None


class _NomicEmbeddingFunction:
    """
    nomic-embed-text-v1 embedding function for ChromaDB.
    Uses 'search_document:' prefix for both storage and queries — sufficient
    for symmetric retrieval over conversational text.
    Max sequence length: 8192 tokens.
    """
    def __init__(self, cache_folder: str):
        from sentence_transformers import SentenceTransformer
        self._model = SentenceTransformer(
            "nomic-ai/nomic-embed-text-v1",
            trust_remote_code=True,
            cache_folder=cache_folder,
        )

    def name(self) -> str:
        return "nomic-embed-text-v1"

    def __call__(self, input: list[str]) -> list[list[float]]:
        prefixed = ["search_document: " + text for text in input]
        return self._model.encode(prefixed, normalize_embeddings=True).tolist()


def get_client() -> chromadb.ClientAPI:
    global _client
    if _client is None:
        _client = chromadb.PersistentClient(path=str(CHROMADB_DIR))
    return _client


def get_collection():
    global _collection
    if _collection is None:
        client = get_client()
        hf_cache = DATA_DIR / "huggingface"
        hf_cache.mkdir(exist_ok=True)
        ef = _NomicEmbeddingFunction(cache_folder=str(hf_cache))
        _collection = client.get_or_create_collection(
            name=CHROMA_COLLECTION,
            embedding_function=ef,
        )
    return _collection
