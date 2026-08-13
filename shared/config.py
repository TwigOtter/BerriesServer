"""
shared/config.py

Loads configuration from environment variables and/or a .env file.
All services import from here — never hardcode secrets or paths.
"""

import os
from pathlib import Path
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

load_dotenv()

# ── Timezone ───────────────────────────────────────────────────────────────
# Calendar-date keying (daily interaction/retrieval logs, dream's "yesterday",
# stream_date labels, transcript filenames) uses this timezone so a "day"
# matches Twig's day, not UTC's. Absolute timestamps stay UTC ISO instants.
LOCAL_TIMEZONE = os.getenv("LOCAL_TIMEZONE", "America/Chicago")
LOCAL_TZ = ZoneInfo(LOCAL_TIMEZONE)

# ── Paths ──────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
TRANSCRIPTS_DIR = DATA_DIR / "transcripts"
CHROMADB_DIR = DATA_DIR / "chromadb"
LOGS_DIR = BASE_DIR / "logs"
PERSONALITY_FILE = BASE_DIR / "berries_bot" / "personality-slim.txt"

# ── ingest_api ─────────────────────────────────────────────────────────────
INGEST_HOST = os.getenv("INGEST_HOST", "0.0.0.0")
INGEST_PORT = int(os.getenv("INGEST_PORT", "8000"))
INGEST_SECRET = os.getenv("INGEST_SECRET", "")       # shared secret header from Streamer.bot

# ── Chunking / buffer ──────────────────────────────────────────────────────
CHUNK_TOKEN_LIMIT = int(os.getenv("CHUNK_TOKEN_LIMIT", "480"))   # flush at ~480 tokens
CHUNK_TIMEOUT_SEC = int(os.getenv("CHUNK_TIMEOUT_SEC", "300"))   # flush after 5 min idle
# Overlap carried into the next chunk after a flush. Token-based, never
# time-based: chat is bursty, and an age cutoff drops the setup for a reply
# that lands minutes later (common in slow Discord watch channels). Must stay
# well under CHUNK_TOKEN_LIMIT or the carried tokens alone re-trip the flush.
CHUNK_OVERLAP_TOKENS = int(os.getenv("CHUNK_OVERLAP_TOKENS", "120"))

# ── ChromaDB ───────────────────────────────────────────────────────────────
CHROMA_COLLECTION = os.getenv("CHROMA_COLLECTION", "stream_transcripts")
CHROMA_N_RESULTS = int(os.getenv("CHROMA_N_RESULTS", "3"))       # chunks to retrieve per query
CHROMA_L2_THRESHOLD = float(os.getenv("CHROMA_L2_THRESHOLD", "0.8"))  # discard chunks with L2 distance above this

# ── Lore retrieval ─────────────────────────────────────────────────────────
# Curated character facts (berries_bot/lore/facts.md) live in their own
# ChromaDB collection with their own slots, so they never compete with the
# ~9k transcript chunks. Retrieval here is deliberately recall-oriented: a
# generous top-n and a lenient distance threshold, no reranking — an
# irrelevant-but-true fact in the prompt is cheap, a missed fact becomes a
# confident fabrication (see berries_bot/lore/README.md).
LORE_COLLECTION = os.getenv("LORE_COLLECTION", "berries_lore")
LORE_N_RESULTS = int(os.getenv("LORE_N_RESULTS", "5"))           # lore entries to retrieve per response
# Measured 2026-07-22 (scripts/eval_lore.py --distances): relevant hits span
# L2 0.62-1.24 while greetings already hit 0.91 — no threshold separates the
# two. 1.5 deliberately admits everything; LORE_N_RESULTS is the real filter,
# and the format_lore framing tells the model to ignore off-topic facts.
LORE_L2_THRESHOLD = float(os.getenv("LORE_L2_THRESHOLD", "1.5"))

# ── Retrieval reranking ────────────────────────────────────────────────────
# After vector search, the assist model scores candidates for relevance to the
# actual message and only chunks scoring >= RERANK_MIN_SCORE are injected
# (possibly none). See shared/retrieval.py.
RERANK_ENABLED = os.getenv("RERANK_ENABLED", "true").lower() in ("1", "true", "yes")
RERANK_CANDIDATES = int(os.getenv("RERANK_CANDIDATES", "12"))    # vector hits fed to the reranker
RERANK_MIN_SCORE = float(os.getenv("RERANK_MIN_SCORE", "5"))     # 0-10; below this a chunk is dropped

# ── Retrieval windowing ────────────────────────────────────────────────────
# Before reranking, each candidate chunk (~480 tokens) is cut down to its most
# query-relevant slice: sliding windows of whole chat lines
# (~WINDOW_TOKEN_LIMIT tokens each, ~50% overlap) are embedded and scored by
# L2 distance against the raw message; the best window merged with its
# better-scoring neighbour (~1.5x the limit) is what the reranker judges and
# what gets injected — so CHROMA_N_RESULTS chunks cost roughly
# N * 1.5 * WINDOW_TOKEN_LIMIT instead of N * 480, keeping the full system
# prompt inside the 4096-token budget. See shared/windowing.py.
# Note: with RERANK_ENABLED=true this also keeps the rerank prompt itself
# inside that budget — turning windowing off puts RERANK_CANDIDATES full
# chunks in one prompt, which overflows 4096 and 400s.
WINDOW_ENABLED = os.getenv("WINDOW_ENABLED", "true").lower() in ("1", "true", "yes")
WINDOW_TOKEN_LIMIT = int(os.getenv("WINDOW_TOKEN_LIMIT", "200"))  # per-window budget; stride is half this
# Address of the chroma-server.service (see deploy/chroma-server.service).
CHROMA_HOST = os.getenv("CHROMA_HOST", "127.0.0.1")
CHROMA_PORT = int(os.getenv("CHROMA_PORT", "8001"))

# ── Embedding microservice ────────────────────────────────────────────────
# Address of the berries-embed.service. Clients (ingest_api, discord_bot,
# dream subprocess, reindex scripts) talk to this instead of loading the
# embedding model into every process.
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "nomic-ai/nomic-embed-text-v1")
EMBED_HOST = os.getenv("EMBED_HOST", "127.0.0.1")
EMBED_PORT = int(os.getenv("EMBED_PORT", "8003"))
EMBED_URL = f"http://{EMBED_HOST}:{EMBED_PORT}"

# ── LLM backend ────────────────────────────────────────────────────────────
# "anthropic" for Anthropic API, "ollama" for local Ollama instance,
# "vllm" for a vLLM server (OpenAI-compatible /v1/chat/completions).
LLM_BACKEND = os.getenv("LLM_BACKEND", "vllm")

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
ANTHROPIC_ASSIST_MODEL = os.getenv("ANTHROPIC_ASSIST_MODEL", "claude-haiku-4-5-20251001")   # query rewriting, gif queries, utility tasks
ANTHROPIC_CHAT_MODEL = os.getenv("ANTHROPIC_CHAT_MODEL", "claude-sonnet-4-6")               # personality/chatbot calls (loads personality.txt)

OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3.1")

VLLM_BASE_URL = os.getenv("VLLM_BASE_URL", "")  # vLLM server base URL, no trailing slash (e.g. http://host:8000)
VLLM_CHAT_MODEL = os.getenv("VLLM_CHAT_MODEL", "")        # served model name; must match what vLLM was launched with
VLLM_ASSIST_MODEL = os.getenv("VLLM_ASSIST_MODEL", "")  # served model name; must match what vLLM was launched with

# vLLM chat-response sampling. Defaults follow Qwen3's recommended non-thinking
# settings; REPETITION_PENALTY (>1.0) is the lever that suppresses verbatim
# copying of in-context blocks -- in vLLM it penalizes prompt tokens too, unlike
# presence/frequency penalties which only see generated tokens. See docs.
# Hard context ceiling of the served model — prompt + completion must fit.
# Raising this past what vLLM was launched with does not buy room; it just
# moves where the 400 happens. Callers that assemble variable-size prompts
# (scripts/dream.py) budget against it explicitly.
VLLM_CONTEXT_TOKENS = int(os.getenv("VLLM_CONTEXT_TOKENS", "4096"))
VLLM_TEMPERATURE = float(os.getenv("VLLM_TEMPERATURE", "0.7"))
VLLM_TOP_P = float(os.getenv("VLLM_TOP_P", "0.8"))
VLLM_TOP_K = int(os.getenv("VLLM_TOP_K", "20"))                       # -1 disables
VLLM_REPETITION_PENALTY = float(os.getenv("VLLM_REPETITION_PENALTY", "1.1"))  # 1.0 = off

# ── Discord ────────────────────────────────────────────────────────────────
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "")
DISCORD_BERRIES_CHANNEL_WHITELIST_IDS: list[int] = [
    int(x) for x in os.getenv("DISCORD_BERRIES_CHANNEL_WHITELIST_IDS", "").split(",")
    if x.strip()
]
DISCORD_WATCH_CHANNEL_IDS: list[int] = [
    int(x) for x in os.getenv("DISCORD_WATCH_CHANNEL_IDS", "").split(",")
    if x.strip()
]
DISCORD_CHUNK_OVERLAP_MESSAGES = int(os.getenv("DISCORD_CHUNK_OVERLAP_MESSAGES", "5"))
_announce_id = os.getenv("DISCORD_ANNOUNCE_CHANNEL_ID", "")
DISCORD_ANNOUNCE_CHANNEL_ID: int | None = int(_announce_id) if _announce_id else None
_berries_chat_id = os.getenv("DISCORD_BERRIES_CHAT_CHANNEL_ID", "")
DISCORD_BERRIES_CHAT_CHANNEL_ID: int | None = int(_berries_chat_id) if _berries_chat_id else None
_log_channel_id = os.getenv("DISCORD_LOG_CHANNEL_ID", "")
DISCORD_LOG_CHANNEL_ID: int | None = int(_log_channel_id) if _log_channel_id else None
DISCORD_BOT_WEBHOOK_PORT = int(os.getenv("DISCORD_BOT_WEBHOOK_PORT", "8002"))
DISCORD_BOT_WEBHOOK_URL = os.getenv("DISCORD_BOT_WEBHOOK_URL", "http://127.0.0.1:8002")
_event_role_id = os.getenv("DISCORD_EVENT_ROLE_ID", "")
DISCORD_EVENT_ROLE_ID: int | None = int(_event_role_id) if _event_role_id else None
_stream_role_id = os.getenv("DISCORD_STREAM_ROLE_ID", "")
DISCORD_STREAM_ROLE_ID: int | None = int(_stream_role_id) if _stream_role_id else None
DISCORD_STICKERS_ONLY_CHANNEL_IDS: list[int] = [
    int(x) for x in os.getenv("DISCORD_STICKERS_ONLY_CHANNEL_IDS", "").split(",")
    if x.strip()
]
_rules_sticker_id = os.getenv("DISCORD_RULES_STICKER_ID", "")
DISCORD_RULES_STICKER_ID: int | None = int(_rules_sticker_id) if _rules_sticker_id else None
DISCORD_CHANNEL_INTERACTION_LIMIT = int(os.getenv("DISCORD_CHANNEL_INTERACTION_LIMIT", "5"))
DISCORD_RESPONSE_TOKENS = int(os.getenv("DISCORD_RESPONSE_TOKENS", "300"))

# ── Agent tools (experimental) ─────────────────────────────────────────────
# When enabled, Discord @mention responses run a tool-use loop (Anthropic
# backend only): the model can search memories, read the server rules, look up
# user profiles, and ping moderators. See shared/agent.py and docs/agent-tools.md.
AGENT_TOOLS_ENABLED = os.getenv("AGENT_TOOLS_ENABLED", "false").lower() in ("1", "true", "yes")
AGENT_MAX_TOOL_ITERATIONS = int(os.getenv("AGENT_MAX_TOOL_ITERATIONS", "3"))
_mod_ping_id = os.getenv("DISCORD_MOD_PING_CHANNEL_ID", "")
DISCORD_MOD_PING_CHANNEL_ID: int | None = int(_mod_ping_id) if _mod_ping_id else None
MOD_PING_COOLDOWN_SEC = int(os.getenv("MOD_PING_COOLDOWN_SEC", "600"))
SERVER_RULES_FILE = BASE_DIR / "berries_bot" / "lore" / "server-rules.md"

# ── Tracing / observability ────────────────────────────────────────────────
# Every response pipeline writes a per-interaction trace (step timings, LLM
# calls, retrieval details, prompts) to logs/traces/YYYY-MM-DD.jsonl and logs
# a one-line summary. Inspect with scripts/traces.py. See shared/trace.py.
TRACE_ENABLED = os.getenv("TRACE_ENABLED", "true").lower() in ("1", "true", "yes")
TRACES_DIR = LOGS_DIR / "traces"

# ── OMDb API ───────────────────────────────────────────────────────────────
OMDB_API_KEY = os.getenv("OMDB_API_KEY", "")

# ── Giphy API ──────────────────────────────────────────────────────────────
GIPHY_API_KEY = os.getenv("GIPHY_API_KEY", "")

# ── Twitch / Streamer.bot ──────────────────────────────────────────────────
STREAMERBOT_CALLBACK_URL = os.getenv("STREAMERBOT_CALLBACK_URL", "")           # URL to POST responses back
STREAMERBOT_RESPONSE_ACTION_ID = os.getenv("STREAMERBOT_RESPONSE_ACTION_ID", "")  # Streamer.bot action to call with Berries' response; set in .env for flexibility but can also be sent in the request body
TWITCH_CHANNEL = os.getenv("TWITCH_CHANNEL", "twigotter")
TWITCH_RESPONSE_TOKENS = int(os.getenv("TWITCH_RESPONSE_TOKENS", "80"))

# ── Conversation history (Twitch/Discord) ─────────────────────────────────
# Short-term memory spliced into `messages` as real user/assistant turns
# (shared/history.py::build_history_turns), trimmed from the oldest end at
# whole-turn granularity. See shared/interactions_db.py::get_recent_twitch_messages
# and discord_bot/cogs/mention.py::_get_channel_history.
TWITCH_HISTORY_TOKEN_LIMIT = int(os.getenv("TWITCH_HISTORY_TOKEN_LIMIT", "960"))  # ~= old recent_chunks ceiling (2 * CHUNK_TOKEN_LIMIT)
TWITCH_HISTORY_ROW_LIMIT = int(os.getenv("TWITCH_HISTORY_ROW_LIMIT", "200"))      # SQL-side pre-filter before the token trim
DISCORD_HISTORY_TOKEN_LIMIT = int(os.getenv("DISCORD_HISTORY_TOKEN_LIMIT", "1028"))  # token-accurate replacement for the old char/4 estimate

# ── Prompt budget (shared/budget.py) ──────────────────────────────────────
# The per-source limits above (history, windowing, lore top-n) each cap one
# input, but nothing capped their SUM -- a long conversation plus fat
# retrieval chunks walked past the model's context ceiling and vLLM answered
# 400, dropping the response entirely. fit_to_budget() is the backstop: it
# costs the assembled prompt and sheds retrieval chunks, then history turns,
# until it fits.
#
# The cost estimate is deliberately conservative. Token counts come from
# tiktoken/cl100k_base (shared/tokenizer.py) but the served model is Qwen,
# and the chat template adds per-message framing that never appears in the
# content -- across 160 logged chat_response calls vLLM's reported
# prompt_tokens ran 101-284 tokens (mean 146, max ratio 1.078) above the raw
# content count. PROMPT_BUDGET_OVERHEAD_RATIO and _PER_MESSAGE below reproduce
# that gap; on those same 160 calls the pair never underestimates. A flat
# additive margin does (14/160), which is why this is a ratio and not a
# constant. Re-derive with scripts/check_prompt_budget.py if the model,
# chat template, or tokenizer changes.
PROMPT_BUDGET_ENABLED = os.getenv("PROMPT_BUDGET_ENABLED", "true").lower() == "true"
PROMPT_BUDGET_OVERHEAD_RATIO = float(os.getenv("PROMPT_BUDGET_OVERHEAD_RATIO", "1.08"))
PROMPT_BUDGET_PER_MESSAGE = int(os.getenv("PROMPT_BUDGET_PER_MESSAGE", "8"))   # chat-template framing per dispatched message
PROMPT_BUDGET_MARGIN = int(os.getenv("PROMPT_BUDGET_MARGIN", "64"))            # final safety gap under the ceiling

# ── Databases ──────────────────────────────────────────────────────────────
USERS_DB_PATH = DATA_DIR / "users.db"
MOVIES_DB_PATH = DATA_DIR / "movies.db"
# Per-event interaction store (docs/sql-interaction-storage.md). Phase 1:
# dual-written alongside the JSONL/Chroma flow; will become the system of
# record that ChromaDB is derived from.
INTERACTIONS_DB_PATH = DATA_DIR / "interactions.db"
