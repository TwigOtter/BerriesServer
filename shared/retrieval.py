"""
shared/retrieval.py

The RAG retrieval stage: everything between a raw user message and the
(document, metadata) pairs injected into the system prompt.

Pipeline (retrieve_context):
  1. rewrite_queries — assist-model rewrite of the message into 2-3 search queries
  2. query_chroma_multi — multi-query vector search (run off the event loop)
  3. rerank_chunks — assist-model relevance scoring over the candidates;
     keeps the best CHROMA_N_RESULTS above RERANK_MIN_SCORE and may return
     nothing at all (abstain) when no candidate is actually relevant
  4. log_retrieval — records the final injected chunks for the nightly
     dreaming summarization and for scripts/eval_retrieval.py

Reranking exists because vector similarity is recall-oriented: a chunk that
merely *mentions* mushrooms scores close to one that is *about* mushrooms.
The cross-check by the assist model is what turns "nearest 4 chunks, always"
into "relevant chunks, or none". Disable with RERANK_ENABLED=false to fall
back to pure vector ordering.
"""

import asyncio
import json
import logging
import re

from shared.config import (
    ANTHROPIC_ASSIST_MODEL,
    CHROMA_N_RESULTS,
    RERANK_CANDIDATES,
    RERANK_ENABLED,
    RERANK_MIN_SCORE,
)
from shared.chroma_client import query_chroma_multi
from shared.llm_client import get_completion
from shared.retrieval_log import log_retrieval

log = logging.getLogger(__name__)


async def rewrite_queries(
    message: str,
    recent_context: str,
    username: str = "a viewer",
) -> list[str]:
    """
    Rewrite `message` into 2-3 focused ChromaDB search queries.
    Always returns a non-empty list — the original message is always included
    so Berries can recall even users who send simple greetings.
    Falls back to [original] on any error.
    """
    prompt = (
        f"Given this recent chat context:\n{recent_context}\n\n"
        f"And this message from {username}:\n\"{message}\"\n\n"
        "Generate 2-3 distinct search queries (one per line, no labels or punctuation) "
        "that capture what information about the user and their query would be "
        "most useful to retrieve in order to respond well to this message. "
        "For short greetings or banter, generate queries about the user by name rather than skipping."
    )
    system = "You generate ChromaDB search queries. Follow the instructions exactly."

    original = f"{username} {message}".strip()
    try:
        raw = await get_completion(system_prompt=system, user_message=prompt, max_tokens=128, model=ANTHROPIC_ASSIST_MODEL)
        queries = [q.strip() for q in raw.strip().splitlines() if q.strip()]
        log.debug("rewrite_queries got parsed queries: %r", queries)
        if not queries:
            return [original]
        # Always include the raw "username message" as a final fallback query
        if original not in queries:
            queries.append(original)
        return queries
    except Exception as e:
        log.warning("rewrite_queries failed, falling back to raw message: %s", e)
        return [original]


_RERANK_SYSTEM = (
    "You judge whether excerpts from a Twitch streamer's chat logs would help an AI chatbot "
    "respond well to a message. Follow the instructions exactly and output only JSON."
)


async def rerank_chunks(
    query: str,
    docs: list[tuple[str, dict]],
    top_k: int = CHROMA_N_RESULTS,
    min_score: float = RERANK_MIN_SCORE,
) -> list[tuple[str, dict]]:
    """
    Score candidate chunks 0-10 for relevance to `query` with the assist model
    and return at most `top_k` of them, best first, dropping anything below
    `min_score`. May return an empty list — that is the abstain path, and it
    means "inject no past context" rather than "inject the least-bad chunks".

    Fails open: if the scoring call or parse fails, returns the first `top_k`
    candidates in vector order so retrieval never breaks because of the judge.
    """
    if not docs:
        return []

    numbered = "\n---\n".join(f"[{i}]\n{doc}" for i, (doc, _meta) in enumerate(docs))
    prompt = (
        f"A user sent this message to the chatbot:\n\"{query}\"\n\n"
        f"Candidate excerpts from past logs:\n{numbered}\n\n"
        "Score each excerpt from 0 to 10 for how useful it would be for responding to the "
        "message. 0 = unrelated, 5 = tangentially related, 10 = directly answers or gives "
        "essential background. Judge usefulness for THIS message — an excerpt that merely "
        "mentions a keyword is not useful.\n\n"
        'Reply with ONLY a JSON object mapping excerpt index to score, e.g. {"0": 7, "1": 2}.'
    )

    try:
        raw = await get_completion(
            system_prompt=_RERANK_SYSTEM,
            user_message=prompt,
            max_tokens=8 * len(docs) + 32,
            model=ANTHROPIC_ASSIST_MODEL,
        )
        match = re.search(r"\{.*\}", raw, flags=re.DOTALL)
        if not match:
            raise ValueError(f"no JSON object in rerank response: {raw[:120]!r}")
        scores = {int(k): float(v) for k, v in json.loads(match.group(0)).items()}
    except Exception as e:
        log.warning("rerank_chunks failed (%s); falling back to vector order", e)
        return docs[:top_k]

    ranked = sorted(range(len(docs)), key=lambda i: scores.get(i, 0.0), reverse=True)
    kept = [docs[i] for i in ranked[:top_k] if scores.get(i, 0.0) >= min_score]
    log.debug(
        "rerank_chunks: kept %d/%d candidates (scores=%s)",
        len(kept), len(docs), {i: scores.get(i, 0.0) for i in ranked},
    )
    return kept


async def retrieve_context(
    query: str,
    recent_context: str,
    username: str,
) -> tuple[list[tuple[str, dict]], list[str]]:
    """
    Full retrieval stage: rewrite → vector search → rerank → log.
    Returns (docs, queries_used). docs is empty on failure or when the
    reranker abstains because nothing retrieved was actually relevant.
    """
    try:
        search_queries = await rewrite_queries(query, recent_context, username)
        n_candidates = RERANK_CANDIDATES if RERANK_ENABLED else CHROMA_N_RESULTS
        # query_chroma_multi blocks on a synchronous embedding HTTP call —
        # run it off the event loop so the bot stays responsive.
        candidates = await asyncio.to_thread(query_chroma_multi, search_queries, n_candidates)
        log.debug(
            "ChromaDB returned %d candidate(s) for %d rewritten query/queries",
            len(candidates), len(search_queries),
        )

        if RERANK_ENABLED:
            docs = await rerank_chunks(query, candidates)
        else:
            docs = candidates[:CHROMA_N_RESULTS]

        # Record what was actually injected, keyed by the original message —
        # feeds the nightly dream summarization and the retrieval eval harness.
        # Summaries are excluded (never re-summarize summaries); lore is
        # excluded (it is curated, not conversational history).
        raw_texts = [doc for doc, meta in docs if meta.get("source") not in ("summary", "lore")]
        if raw_texts:
            await asyncio.to_thread(log_retrieval, query=query, chunks=raw_texts)

        return docs, search_queries
    except Exception:
        log.exception("Retrieval failed (no context injected)")
        return [], []
