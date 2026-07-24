"""
tests/test_interactions_db.py

Tests for the Phase-1 per-event interaction store (shared/interactions_db.py),
against a temp database via monkeypatched INTERACTIONS_DB_PATH.
"""

import json

import pytest

import shared.interactions_db as idb


@pytest.fixture(autouse=True)
def _tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(idb, "INTERACTIONS_DB_PATH", tmp_path / "interactions.db")
    idb.init_db()


def _rows(table: str) -> list[dict]:
    with idb._connect() as conn:
        return [dict(r) for r in conn.execute(f"SELECT * FROM {table} ORDER BY id")]


def test_init_db_is_idempotent():
    idb.init_db()
    idb.init_db()
    assert _rows("twitch_events") == []
    assert _rows("discord_messages") == []


def test_twitch_message_roundtrip():
    idb.log_twitch_event(
        type="message",
        content="hello berries!",
        user_id=424960237,
        username="chatter123",
        display_name="Chatter123",
        stream_title="Cozy Chaos",
        stream_category="Games & Demos",
        message_id="msg-1",
        payload={"bits": 100, "role": "VIP"},
    )
    (row,) = _rows("twitch_events")
    assert row["type"] == "message"
    assert row["content"] == "hello berries!"
    assert row["user_id"] == 424960237
    assert row["stream_title"] == "Cozy Chaos"
    assert json.loads(row["payload"]) == {"bits": 100, "role": "VIP"}
    assert row["is_bot"] == 0 and row["invoked_berries"] == 0
    assert row["created_at"] and row["stream_date"]


def test_twitch_duplicate_message_id_ignored():
    for _ in range(2):
        idb.log_twitch_event(type="message", content="same", message_id="dup-1")
    assert len(_rows("twitch_events")) == 1


def test_twitch_events_without_message_id_all_kept():
    idb.log_twitch_event(type="speech", content="hello chat")
    idb.log_twitch_event(type="speech", content="welcome in")
    assert len(_rows("twitch_events")) == 2


def test_twitch_write_failure_is_swallowed(monkeypatch):
    def boom():
        raise RuntimeError("db unavailable")

    monkeypatch.setattr(idb, "_connect", boom)
    idb.log_twitch_event(type="message", content="never stored")  # must not raise


def test_discord_message_roundtrip():
    idb.log_discord_message(
        channel_id="123",
        channel_name="berries-chat",
        guild_id="9",
        user_id="42",
        username="twigotter",
        display_name="Twig",
        message_id="m-1",
        message_text="hey @BerriesTheDemon",
        reply_to_message_id="m-0",
        invoked_berries=True,
        created_at="2026-07-22T03:20:23+00:00",
    )
    (row,) = _rows("discord_messages")
    assert row["message_text"] == "hey @BerriesTheDemon"
    assert row["invoked_berries"] == 1
    assert row["created_at"] == "2026-07-22T03:20:23+00:00"
    assert row["reply_to_message_id"] == "m-0"


def test_discord_upsert_escalates_invoked_flag():
    # Watcher writes first without the flag, mention cog second with it.
    idb.log_discord_message(channel_id="1", user_id="42", message_id="m-1", message_text="hi")
    idb.log_discord_message(channel_id="1", user_id="42", message_id="m-1", message_text="hi", invoked_berries=True)
    (row,) = _rows("discord_messages")
    assert row["invoked_berries"] == 1


def test_discord_upsert_never_downgrades_invoked_flag():
    # Mention cog first, watcher second — flag must survive.
    idb.log_discord_message(channel_id="1", user_id="42", message_id="m-1", message_text="hi", invoked_berries=True)
    idb.log_discord_message(channel_id="1", user_id="42", message_id="m-1", message_text="hi")
    (row,) = _rows("discord_messages")
    assert row["invoked_berries"] == 1


def test_discord_bot_reply_recorded_with_is_bot():
    idb.log_discord_message(
        channel_id="1", user_id="99", username="berriesthedemon",
        message_id="m-2", message_text="The sigil approves.", is_bot=True,
    )
    (row,) = _rows("discord_messages")
    assert row["is_bot"] == 1
