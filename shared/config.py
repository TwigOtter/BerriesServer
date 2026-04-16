"""
shared/config.py

Loads configuration from environment variables and/or a .env file.
All services import from here — never hardcode secrets or paths.
"""

import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

# ── Paths ──────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
TRANSCRIPTS_DIR = DATA_DIR / "transcripts"
CHROMADB_DIR = DATA_DIR / "chromadb"
LOGS_DIR = BASE_DIR / "logs"
PERSONALITY_FILE = BASE_DIR / "berries_bot" / "personality.txt"

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
CHROMA_N_RESULTS = int(os.getenv("CHROMA_N_RESULTS", "4"))       # chunks to retrieve per query
CHROMA_COSINE_THRESHOLD = float(os.getenv("CHROMA_COSINE_THRESHOLD", "0.32"))  # discard chunks with cosine distance above this (0.32 ≈ cosine similarity < 0.68)

# ── Embedding model ────────────────────────────────────────────────────────
# Uses sentence-transformers locally — no data leaves the box.
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "nomic-ai/nomic-embed-text-v1")

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
