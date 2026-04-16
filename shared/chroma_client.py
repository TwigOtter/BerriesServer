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
from shared.config import CHROMADB_DIR, CHROMA_COLLECTION, DATA_DIR, CHROMA_N_RESULTS, CHROMA_L2_THRESHOLD

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

    def embed_query(self, input: list[str]) -> list[list[float]]:
        prefixed = ["search_query: " + text for text in input]
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


def query_chroma_multi(queries: list[str], n_results: int = CHROMA_N_RESULTS) -> list[tuple[str, dict]]:
    """
    Run all queries against ChromaDB in one call, deduplicate by chunk ID,
    and return at most `n_results` unique (document, metadata) pairs.

    Results are interleaved round-robin by rank across queries (best from each
    query first, then 2nd-best from each, etc.) so that each query is guaranteed
    at least one result before any query claims a second slot.
    """
    if not queries:
        return []
    collection = get_collection()
    results = collection.query(
        query_texts=queries,
        n_results=n_results,
        include=["documents", "metadatas", "distances"],
    )

    # Example `results` structure for 2 queries and n_results=3:
    # {
    #     "ids": [
    #         ["chunk_001", "chunk_005", "chunk_012"],  # top 3 results for query 1
    #         ["chunk_003", "chunk_001", "chunk_008"],  # top 3 results for query 2
    #     ],
    #     "documents": [
    #         ["text of chunk 1...", "text of chunk 5...", "text of chunk 12..."],
    #         ["text of chunk 3...", "text of chunk 1...", "text of chunk 8..."],
    #     ],
    #     "metadatas": [
    #         [{"stream_date": "2025-11-14", ...}, ...],  # metadata for query 1 results
    #         [{"stream_date": "2025-11-14", ...}, ...],  # metadata for query 2 results
    #     ],
    #     "distances": [
    #         [0.42, 0.71, 1.05],  # L2 distances for query 1 (lower = more similar)
    #         [0.38, 0.79, 0.91],  # L2 distances for query 2
    #     ]
    # }

    # Build per-query ranked lists of (chunk_id, doc, metadata) triples,
    # filtering out any chunk whose L2 distance exceeds the threshold.
    per_query: list[list[tuple[str, str, dict]]] = [
        [
            (chunk_id, doc, meta)
            for chunk_id, doc, meta, dist in zip(id_list, doc_list, meta_list, dist_list)
            if dist <= CHROMA_L2_THRESHOLD
        ]
        for id_list, doc_list, meta_list, dist_list in zip(
            results.get("ids", []),
            results.get("documents", []),
            results.get("metadatas", []),
            results.get("distances", []),
        )
    ]

    # `per_query` is now:
    # [
    #   [("chunk_001", "text...", {...}), ("chunk_005", "text...", {...}), ...],  <- query 1
    #   [("chunk_003", "text...", {...}), ("chunk_001", "text...", {...}), ...],  <- query 2
    # ]

    # Interleave round-robin: rank-0 from each query, then rank-1, etc.
    # Stops as soon as n_results unique chunks are collected.
    seen_ids: set[str] = set()
    docs: list[tuple[str, dict]] = []
    for rank in range(n_results):
        for query_results in per_query:
            if len(docs) >= n_results:
                return docs
            if rank < len(query_results):
                chunk_id, doc, meta = query_results[rank]
                if chunk_id not in seen_ids:
                    seen_ids.add(chunk_id)
                    docs.append((doc, meta))
    return docs
