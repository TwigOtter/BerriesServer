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
    LORE_COLLECTION,
    LORE_L2_THRESHOLD,
    LORE_N_RESULTS,
)

_client: chromadb.ClientAPI | None = None
_collection = None
_lore_collection = None
_ef = None


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


def _embedding_function() -> _NomicEmbeddingFunction:
    global _ef
    if _ef is None:
        _ef = _NomicEmbeddingFunction(embed_url=EMBED_URL)
    return _ef


def get_collection():
    """Shared transcript/summary/Discord collection."""
    global _collection
    if _collection is None:
        _collection = get_client().get_or_create_collection(
            name=CHROMA_COLLECTION,
            embedding_function=_embedding_function(),
        )
    return _collection


def get_lore_collection():
    """
    Dedicated collection for curated lore (berries_bot/lore/facts.md).

    Separate from the transcript collection so that lore never competes with
    ~9k transcript chunks for retrieval slots, lore edits never open the
    transcript index for writing, and its distance threshold tunes
    independently — see berries_bot/lore/README.md.

    ⚠️ Must use the same nomic embedding function as the main collection. A
    collection created with Chroma's default embedder raises no error — it
    silently returns garbage rankings. Always create it here, never via a
    fresh Client().
    """
    global _lore_collection
    if _lore_collection is None:
        _lore_collection = get_client().get_or_create_collection(
            name=LORE_COLLECTION,
            embedding_function=_embedding_function(),
        )
    return _lore_collection


def embed_documents(texts: list[str]) -> list[np.ndarray]:
    """
    Embed texts on the 'search_document:' side of nomic's asymmetric pair —
    the same path chunk documents take at index time. Used by
    shared/windowing.py to score sub-chunk windows against a query.
    """
    return _embedding_function()(texts)


def embed_query(text: str) -> np.ndarray:
    """Embed one query on the 'search_query:' side of the asymmetric pair."""
    return _embedding_function().embed_query([text])[0]


def upsert_summary(chunk_id: str, text: str, metadata: dict) -> None:
    """Write or overwrite a source:summary entry in ChromaDB."""
    collection = get_collection()
    collection.upsert(ids=[chunk_id], documents=[text], metadatas=[metadata])


def query_chroma_multi(queries: list[str], n_results: int = CHROMA_N_RESULTS) -> list[tuple[str, dict]]:
    """
    Run all queries against the shared transcript collection in one call,
    deduplicate by chunk ID, and return at most `n_results` unique
    (document, metadata) pairs.

    Results are interleaved round-robin by rank across queries (best from each
    query first, then 2nd-best from each, etc.) so that each query is guaranteed
    at least one result before any query claims a second slot.

    Curated lore does not live in this collection — it has its own
    (get_lore_collection / query_lore_multi) so it never competes with
    transcript chunks for these slots.
    """
    if not queries:
        return []
    results = get_collection().query(
        query_texts=queries,
        n_results=n_results,
        include=["documents", "metadatas", "distances"],
    )
    return _interleave_results(results, n_results, CHROMA_L2_THRESHOLD)


def query_lore_multi(queries: list[str], n_results: int = LORE_N_RESULTS) -> list[tuple[str, dict]]:
    """
    Run all queries against the dedicated lore collection and return at most
    `n_results` unique (document, metadata) pairs, interleaved round-robin.

    Deliberately recall-oriented: generous top-n, lenient LORE_L2_THRESHOLD,
    and callers do not rerank. Against ~20 curated entries the cost of a false
    positive is a paragraph of irrelevant-but-true lore; the cost of a false
    negative is Berries confidently inventing a character detail
    (berries_bot/lore/README.md, measured 2026-07-15).
    """
    if not queries:
        return []
    results = get_lore_collection().query(
        query_texts=queries,
        n_results=n_results,
        include=["documents", "metadatas", "distances"],
    )
    return _interleave_results(results, n_results, LORE_L2_THRESHOLD)


def _interleave_results(results: dict, n_results: int, l2_threshold: float) -> list[tuple[str, dict]]:
    """
    Merge a multi-query Chroma result dict into at most `n_results` unique
    (document, metadata) pairs, dropping anything above `l2_threshold`.

    Example `results` structure for 2 queries and n_results=3:

        {
            "ids": [
                ["chunk_001", "chunk_005", "chunk_012"],  # top 3 results for query 1
                ["chunk_003", "chunk_001", "chunk_008"],  # top 3 results for query 2
            ],
            "documents": [
                ["text of chunk 1...", "text of chunk 5...", "text of chunk 12..."],
                ["text of chunk 3...", "text of chunk 1...", "text of chunk 8..."],
            ],
            "metadatas": [
                [{"stream_date": "2025-11-14", ...}, ...],  # metadata for query 1 results
                [{"stream_date": "2025-11-14", ...}, ...],  # metadata for query 2 results
            ],
            "distances": [
                [0.42, 0.71, 1.05],  # L2 distances for query 1 (lower = more similar)
                [0.38, 0.79, 0.91],  # L2 distances for query 2
            ]
        }
    """
    # Build per-query ranked lists of (chunk_id, doc, metadata) triples,
    # filtering out any chunk whose L2 distance exceeds the threshold.
    per_query: list[list[tuple[str, str, dict]]] = [
        [
            (chunk_id, doc, meta)
            for chunk_id, doc, meta, dist in zip(id_list, doc_list, meta_list, dist_list)
            if dist <= l2_threshold
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
