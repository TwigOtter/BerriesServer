"""
tests/test_ingest_api.py

Integration tests for ingest_api endpoints.
The FastAPI app is tested in-process via httpx.AsyncClient — no server needed.

Mocked dependencies:
  - shared.llm_client.get_completion  — avoids real API calls
  - ingest_api.main.get_collection    — avoids real ChromaDB
  - ingest_api.main._post_to_streamerbot — avoids real Streamer.bot HTTP
  - shared.user_db.init_db / upsert_user — avoids touching data/users.db
"""

import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, MagicMock, patch
from httpx import AsyncClient, ASGITransport


# ── Fixtures ───────────────────────────────────────────────────────────────

@pytest.fixture
def mock_collection():
    col = MagicMock()
    col.query.return_value = {"documents": [[]]}
    return col


@pytest_asyncio.fixture
async def client(mock_collection, tmp_path):
    """
    Spin up the ingest_api app in-process for each test.
    Patches out all I/O: LLM, ChromaDB, Streamer.bot, and SQLite user DB.
    Resets module-level buffer state between tests.
    """
    patches = [
        patch("shared.llm_client.get_completion", new=AsyncMock(return_value="spooky test response")),
        patch("ingest_api.main.get_collection", return_value=mock_collection),
        patch("ingest_api.main._post_to_streamerbot", new=AsyncMock()),
        patch("shared.user_db.init_db"),
        patch("shared.user_db.upsert_user"),
        patch("ingest_api.main.USERS_DB_PATH", tmp_path / "users.db"),
        patch("ingest_api.main.TRANSCRIPTS_DIR", tmp_path / "transcripts"),
    ]
    for p in patches:
        p.start()

    # Import after patches are active so module-level state is clean
    from ingest_api.main import app, _buffer, recent_chunks
    _buffer.clear()
    recent_chunks.clear()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c

    for p in patches:
        p.stop()


# ── /event/chat ─────────────────────────────────────────────────────────────

class TestChatEvent:
    async def test_ok_response(self, client):
        resp = await client.post("/event/chat", json={
            "userName": "viewer123",
            "displayName": "Viewer123",
            "message": "hello berries!",
            "messageStripped": "hello berries!",
            "emoteCount": "0",
            "role": "1",
        })
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

    async def test_empty_message_is_dropped(self, client):
        resp = await client.post("/event/chat", json={
            "userName": "viewer123",
            "displayName": "Viewer123",
            "message": "   ",
            "messageStripped": "",
            "emoteCount": "0",
            "role": "1",
        })
        assert resp.status_code == 200
        assert resp.json()["status"] == "dropped"

    async def test_message_buffered(self, client):
        from ingest_api.main import _buffer
        _buffer.clear()
        await client.post("/event/chat", json={
            "userName": "viewer123",
            "displayName": "Viewer123",
            "message": "what's your favorite berry?",
            "messageStripped": "what's your favorite berry?",
            "emoteCount": "0",
            "role": "1",
        })
        assert len(_buffer) == 1
        assert "viewer123" in _buffer[0]["text"]

    async def test_emote_condensing(self, client):
        """Repeated emotes should be condensed: 'PogChamp PogChamp PogChamp' → 'PogChamp x3'."""
        from ingest_api.main import _buffer
        _buffer.clear()
        await client.post("/event/chat", json={
            "userName": "viewer123",
            "displayName": "Viewer123",
            "message": "nice PogChamp PogChamp PogChamp",
            "messageStripped": "nice",
            "emoteCount": "3",
            "role": "1",
        })
        assert "PogChamp x3" in _buffer[0]["text"]

    async def test_subscriber_flags_logged(self, client):
        """Sub tier info should be accepted without error."""
        resp = await client.post("/event/chat", json={
            "userName": "subguy",
            "displayName": "SubGuy",
            "message": "love this stream",
            "messageStripped": "love this stream",
            "emoteCount": "0",
            "role": "1",
            "isSubscribed": "true",
            "subscriptionTier": "1000",
            "monthsSubscribed": "6",
        })
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"


# ── /event/speech ───────────────────────────────────────────────────────────

class TestSpeechEvent:
    async def test_ok_response(self, client):
        resp = await client.post("/event/speech", json={
            "speaker": "TwigOtter",
            "text": "welcome to the stream everyone!",
        })
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

    async def test_speech_buffered(self, client):
        from ingest_api.main import _buffer
        _buffer.clear()
        await client.post("/event/speech", json={
            "speaker": "TwigOtter",
            "text": "let's get started",
        })
        assert len(_buffer) == 1
        assert "TwigOtter" in _buffer[0]["text"]

    async def test_empty_speech_dropped(self, client):
        resp = await client.post("/event/speech", json={"speaker": "TwigOtter", "text": ""})
        assert resp.json()["status"] == "dropped"


# ── /event/stream-update ────────────────────────────────────────────────────

class TestStreamUpdate:
    async def test_updates_metadata(self, client):
        resp = await client.post("/event/stream-update", json={
            "title": "Cozy Chaos",
            "category": "Games & Demos",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["stream_metadata"]["title"] == "Cozy Chaos"
        assert data["stream_metadata"]["category"] == "Games & Demos"


# ── /event/mention ──────────────────────────────────────────────────────────

class TestMentionEvent:
    async def test_returns_llm_response(self, client):
        resp = await client.post("/event/mention", json={
            "text": "what do you think about mushrooms?",
            "username": "viewer123",
            "CHAT": True,
            "TTS": False,
        })
        assert resp.status_code == 200
        assert resp.json()["message"] == "spooky test response"

    async def test_empty_text_returns_not_triggered(self, client):
        resp = await client.post("/event/mention", json={
            "text": "",
            "username": "viewer123",
        })
        assert resp.status_code == 200
        assert resp.json()["triggered"] is False

    async def test_tts_flag_propagated(self, client):
        resp = await client.post("/event/mention", json={
            "text": "say something spooky",
            "TTS": True,
            "CHAT": False,
        })
        assert resp.json()["TTS"] is True

    async def test_twitch_tts_context_includes_prosody_instructions(self, client):
        """TTS=True should produce a system prompt with prosody tag instructions."""
        with patch("shared.llm_client.get_completion", new=AsyncMock(return_value="boo")) as mock_llm:
            await client.post("/event/mention", json={
                "text": "say something dramatic",
                "TTS": True,
            })
        system_prompt = mock_llm.call_args.kwargs["system_prompt"]
        assert "prosody" in system_prompt.lower()

    async def test_twitch_chat_context_excludes_prosody_instructions(self, client):
        """TTS=False should produce a system prompt without prosody tag instructions."""
        with patch("shared.llm_client.get_completion", new=AsyncMock(return_value="boo")) as mock_llm:
            await client.post("/event/mention", json={
                "text": "say something",
                "TTS": False,
            })
        system_prompt = mock_llm.call_args.kwargs["system_prompt"]
        assert "prosody" not in system_prompt.lower()

    async def test_system_prompt_forbids_markdown_on_twitch(self, client):
        with patch("shared.llm_client.get_completion", new=AsyncMock(return_value="boo")) as mock_llm:
            await client.post("/event/mention", json={"text": "hello"})
        system_prompt = mock_llm.call_args.kwargs["system_prompt"]
        assert "markdown" in system_prompt.lower()

    async def test_chroma_context_injected_when_available(self, client, mock_collection):
        mock_collection.query.return_value = {
            "documents": [["Berries once hid in Twig's streaming chair."]]
        }
        with patch("shared.llm_client.get_completion", new=AsyncMock(return_value="boo")) as mock_llm:
            await client.post("/event/mention", json={"text": "what are you up to?"})
        system_prompt = mock_llm.call_args.kwargs["system_prompt"]
        assert "Berries once hid in Twig's streaming chair." in system_prompt

    async def test_chroma_failure_does_not_break_response(self, client, mock_collection):
        mock_collection.query.side_effect = Exception("chroma is down")
        resp = await client.post("/event/mention", json={"text": "hello berries"})
        assert resp.status_code == 200
        assert resp.json()["message"] == "spooky test response"


# ── /health ─────────────────────────────────────────────────────────────────

class TestHealth:
    async def test_health_ok(self, client):
        resp = await client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

    async def test_health_reports_buffer_size(self, client):
        from ingest_api.main import _buffer
        _buffer.clear()
        await client.post("/event/chat", json={
            "userName": "viewer123",
            "displayName": "Viewer123",
            "message": "hello!",
            "messageStripped": "hello!",
            "emoteCount": "0",
            "role": "1",
        })
        resp = await client.get("/health")
        assert resp.json()["buffer_entries"] == 1


# ── Auth ────────────────────────────────────────────────────────────────────

class TestAuth:
    async def test_correct_secret_accepted(self, client):
        with patch("ingest_api.main.INGEST_SECRET", "mysecret"):
            resp = await client.post(
                "/event/chat",
                json={"userName": "u", "displayName": "U", "message": "hi", "messageStripped": "hi", "emoteCount": "0", "role": "1"},
                headers={"x-secret": "mysecret"},
            )
        assert resp.status_code == 200

    async def test_wrong_secret_rejected(self, client):
        with patch("ingest_api.main.INGEST_SECRET", "mysecret"):
            resp = await client.post(
                "/event/chat",
                json={"userName": "u", "displayName": "U", "message": "hi", "messageStripped": "hi", "emoteCount": "0", "role": "1"},
                headers={"x-secret": "wrongsecret"},
            )
        assert resp.status_code == 403

    async def test_no_secret_configured_allows_all(self, client):
        with patch("ingest_api.main.INGEST_SECRET", ""):
            resp = await client.post(
                "/event/chat",
                json={"userName": "u", "displayName": "U", "message": "hi", "messageStripped": "hi", "emoteCount": "0", "role": "1"},
            )
        assert resp.status_code == 200
