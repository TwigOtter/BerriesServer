"""
embed_api/main.py

Lightweight FastAPI service that exposes nomic-embed-text-v1 embeddings.
Loads the model once at startup; clients call this service over HTTP rather
than each loading their own ~2GB copy of the model.

Endpoints:
    POST /embed/documents — embeds with 'search_document:' prefix (storage)
    POST /embed/queries   — embeds with 'search_query:' prefix   (retrieval)
    GET  /health          — readiness check; 200 OK once model is loaded

Body:     {"texts": ["..."]}
Response: {"embeddings": [[float, ...], ...]}
"""

import logging
import os
from contextlib import asynccontextmanager

# Suppress HuggingFace tokenizer fork warning before any tokenizer imports.
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import torch

from shared.config import DATA_DIR, EMBEDDING_MODEL

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("embed_api")

_model = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Load the SentenceTransformer model on startup; held for process lifetime."""
    global _model
    from sentence_transformers import SentenceTransformer
    cache_folder = str(DATA_DIR / "huggingface")
    log.info("Loading %s (cache=%s)...", EMBEDDING_MODEL, cache_folder)
    if torch.cuda.is_available():
        device = "cuda"
        log.info("CUDA available — embedding on GPU (%s)", torch.cuda.get_device_name(0))
    else:
        device = "cpu"
        log.warning(
            "CUDA unavailable — falling back to CPU. Embedding will be slow. "
            "Check that nvidia drivers are loaded and the service can see "
            "/dev/nvidia* (PrivateDevices=no in the unit file)."
        )
    _model = SentenceTransformer(
        EMBEDDING_MODEL,
        trust_remote_code=True,
        cache_folder=cache_folder,
        device=device,
    )
    log.info("Model ready on %s; embed_api accepting requests", device)
    yield
    log.info("Shutting down embed_api")


app = FastAPI(lifespan=lifespan, title="Berries Embed API")


class EmbedRequest(BaseModel):
    texts: list[str]


class EmbedResponse(BaseModel):
    embeddings: list[list[float]]


@app.get("/health")
async def health() -> dict:
    return {"ok": _model is not None}


def _encode(texts: list[str], prefix: str) -> list[list[float]]:
    if _model is None:
        raise HTTPException(status_code=503, detail="model not loaded")
    if not texts:
        return []
    prefixed = [prefix + t for t in texts]
    return _model.encode(prefixed, normalize_embeddings=True, show_progress_bar=False).tolist()


@app.post("/embed/documents", response_model=EmbedResponse)
async def embed_documents(req: EmbedRequest) -> EmbedResponse:
    return EmbedResponse(embeddings=_encode(req.texts, "search_document: "))


@app.post("/embed/queries", response_model=EmbedResponse)
async def embed_queries(req: EmbedRequest) -> EmbedResponse:
    return EmbedResponse(embeddings=_encode(req.texts, "search_query: "))
