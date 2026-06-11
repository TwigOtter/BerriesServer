"""
shared/chroma_client.py

Singleton ChromaDB client shared across services.

Architecture:
  - chroma-server.service (deploy/chroma-server.service) is the single
    writer/reader of the SQLite store. We connect to it via HttpClient.
  - berries-embed.service (deploy/berries-embed.service) hosts the
    nomic-embed-text-v1 model. Our embedding function below POSTs text
    to it instead of loading the model in-process — keeps every client
    process slim and avoids loading the same 2GB model N times.

Usage:
    from shared.chroma_client import get_collection
    collection = get_collection()
    collection.add(documents=["..."], ids=["chunk_001"])
    results = collection.query(query_texts=["what did Twig say about disc golf?"], n_results=4)
"""

import httpx
import numpy as np
import chromadb

from shared.config import (
    CHROMA_COLLECTION,
    CHROMA_HOST,
    CHROMA_PORT,
    CHROMA_N_RESULTS,
    CHROMA_L2_THRESHOLD,
    EMBED_URL,
)

_client: chromadb.ClientAPI | None = None
_collection = None


class _NomicEmbeddingFunction:
    """
    Calls the berries-embed microservice over HTTP to embed documents/queries.
    nomic-embed-text-v1 uses asymmetric retrieval, so documents and queries
    get different prefixes ('search_document:' vs 'search_query:'). The
    microservice handles the prefixing — this class just routes to the
    correct endpoint.
    """
    def __init__(self, embed_url: str):
        self._url = embed_url.rstrip("/")
        # 5-minute read timeout — large reindex batches on CPU can take
        # tens of seconds; 30s was too tight. GPU never gets close.
        # httpx.Client is thread-safe and reuses connections.
        self._http = httpx.Client(base_url=self._url, timeout=300.0)

    def name(self) -> str:
        return "nomic-embed-text-v1"

    def __call__(self, input: list[str]) -> list[np.ndarray]:
        return self._post("/embed/documents", input)

    def embed_query(self, input: list[str]) -> list[np.ndarray]:
        return self._post("/embed/queries", input)

    def _post(self, path: str, texts: list[str]) -> list[np.ndarray]:
        resp = self._http.post(path, json={"texts": list(texts)})
        resp.raise_for_status()
        # ChromaDB's HttpClient query path calls .tolist() on each embedding —
        # it expects numpy arrays, not plain lists. Convert here at the JSON
        # boundary so all chromadb callers get the type they expect.
        return [np.asarray(emb, dtype=np.float32) for emb in resp.json()["embeddings"]]


def get_client() -> chromadb.ClientAPI:
    global _client
    if _client is None:
        _client = chromadb.HttpClient(host=CHROMA_HOST, port=CHROMA_PORT)
    return _client


def get_collection():
    global _collection
    if _collection is None:
        client = get_client()
        ef = _NomicEmbeddingFunction(embed_url=EMBED_URL)
        _collection = client.get_or_create_collection(
            name=CHROMA_COLLECTION,
            embedding_function=ef,
        )
    return _collection


def upsert_summary(chunk_id: str, text: str, metadata: dict) -> None:
    """Write or overwrite a source:summary entry in ChromaDB."""
    collection = get_collection()
    collection.upsert(ids=[chunk_id], documents=[text], metadatas=[metadata])


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
