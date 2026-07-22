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
CHUNK_OVERLAP_SEC = int(os.getenv("CHUNK_OVERLAP_SEC", "30"))    # keep last 30s on flush

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
# After reranking, each kept chunk (~480 tokens) is cut down to its most
# query-relevant slice before injection: sliding windows of whole chat lines
# (~WINDOW_TOKEN_LIMIT tokens each, ~50% overlap) are embedded and scored by
# L2 distance against the raw message; the best window merged with its
# better-scoring neighbour (~150 tokens) is what gets injected. Keeps the
# chroma block near ~600 tokens instead of ~1600 so the full system prompt
# fits the 4096-token budget. See shared/windowing.py.
WINDOW_ENABLED = os.getenv("WINDOW_ENABLED", "true").lower() in ("1", "true", "yes")
WINDOW_TOKEN_LIMIT = int(os.getenv("WINDOW_TOKEN_LIMIT", "100"))  # per-window budget; stride is half this
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
# "anthropic" for Anthropic API, "ollama" for local Ollama instance.
LLM_BACKEND = os.getenv("LLM_BACKEND", "anthropic")

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
ANTHROPIC_ASSIST_MODEL = os.getenv("ANTHROPIC_ASSIST_MODEL", "claude-haiku-4-5-20251001")   # query rewriting, gif queries, utility tasks
ANTHROPIC_CHAT_MODEL = os.getenv("ANTHROPIC_CHAT_MODEL", "claude-sonnet-4-6")               # personality/chatbot calls (loads personality.txt)

OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3.1")

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

# ── Databases ──────────────────────────────────────────────────────────────
USERS_DB_PATH = DATA_DIR / "users.db"
MOVIES_DB_PATH = DATA_DIR / "movies.db"
# Per-event interaction store (docs/sql-interaction-storage.md). Phase 1:
# dual-written alongside the JSONL/Chroma flow; will become the system of
# record that ChromaDB is derived from.
INTERACTIONS_DB_PATH = DATA_DIR / "interactions.db"
