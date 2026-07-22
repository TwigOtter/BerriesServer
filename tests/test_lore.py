"""
tests/test_lore.py

Tests for the lore file parser in scripts/reindex_lore.py, the retrieved-lore
prompt block, and LoreProvider's retrieval behavior.
"""

from pathlib import Path

import scripts.reindex_lore as reindex_lore
from scripts.reindex_lore import parse_lore_file
from shared.context_providers import BerriesRequest, LoreProvider
from shared.prompt_builder import format_lore


def _write(tmp_path: Path, content: str) -> Path:
    path = tmp_path / "facts.md"
    path.write_text(content, encoding="utf-8")
    return path


def test_sections_become_entries(tmp_path):
    path = _write(tmp_path, (
        "# Berries facts\n"
        "intro text that belongs to no section\n\n"
        "## Food\nBerries is a vegetarian.\n\n"
        "## The forest\nDark. Damp.\nHome.\n"
    ))
    entries = parse_lore_file(path)
    assert [e["id"] for e in entries] == ["lore_facts_food", "lore_facts_the-forest"]
    assert entries[0]["document"] == "Food\nBerries is a vegetarian."
    assert entries[1]["document"] == "The forest\nDark. Damp.\nHome."
    assert entries[0]["metadata"] == {"source": "lore", "title": "Food", "file": "facts.md"}


def test_empty_sections_are_skipped(tmp_path):
    path = _write(tmp_path, "## Empty\n\n## Real\ncontent\n")
    entries = parse_lore_file(path)
    assert [e["metadata"]["title"] for e in entries] == ["Real"]


def test_heading_slugs_are_safe(tmp_path):
    path = _write(tmp_path, "## What's Berries' favorite food?!\nberries, obviously\n")
    entries = parse_lore_file(path)
    assert entries[0]["id"] == "lore_facts_what-s-berries-favorite-food"


def test_file_without_sections_yields_nothing(tmp_path):
    path = _write(tmp_path, "just some prose\nwith no headings\n")
    assert parse_lore_file(path) == []


def test_readme_and_server_rules_are_not_indexed(tmp_path, monkeypatch):
    (tmp_path / "facts.md").write_text("## Food\nBerries is a vegetarian.\n", encoding="utf-8")
    (tmp_path / "server-rules.md").write_text("## Hard Rules\nBe kind.\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("## How it works\ndocs, not lore\n", encoding="utf-8")
    monkeypatch.setattr(reindex_lore, "LORE_DIR", tmp_path)
    entries = reindex_lore.collect_entries()
    assert [e["id"] for e in entries] == ["lore_facts_food"]


# ── format_lore ────────────────────────────────────────────────────────────

def test_format_lore_joins_retrieved_entries():
    docs = [
        ("Food\nBerries is a vegetarian.", {"source": "lore", "title": "Food"}),
        ("Fear of water\nRunning water weakens Berries.", {"source": "lore", "title": "Fear of water"}),
    ]
    block = format_lore(docs)
    assert block.startswith("CHARACTER FACTS:")
    assert "Berries is a vegetarian." in block
    assert "Running water weakens Berries." in block
    assert "\n---\n" in block


# ── LoreProvider ───────────────────────────────────────────────────────────

class TestLoreProvider:
    async def test_queries_with_message_and_lore_context(self, monkeypatch):
        seen: list[list[str]] = []

        def fake_query(queries):
            seen.append(queries)
            return [("Food\nBerries is a vegetarian.", {"source": "lore", "title": "Food"})]

        monkeypatch.setattr("shared.context_providers.query_lore_multi", fake_query)
        req = BerriesRequest(query="what do you eat?", lore_context="chat about dinner")
        block = await LoreProvider().provide(req)
        assert seen == [["what do you eat?", "chat about dinner"]]
        assert "Berries is a vegetarian." in block

    async def test_recent_context_does_not_leak_into_lore_query(self, monkeypatch):
        """Berries' own messages live in recent_context (channel history) — the
        lore query must only use lore_context, or his voice steers retrieval."""
        seen: list[list[str]] = []

        def fake_query(queries):
            seen.append(queries)
            return []

        monkeypatch.setattr("shared.context_providers.query_lore_multi", fake_query)
        req = BerriesRequest(
            query="hello",
            recent_context="BerriesTheDemon: The Ledger notes this.",
            lore_context="",
        )
        await LoreProvider().provide(req)
        assert seen == [["hello"]]

    async def test_no_hits_yields_no_block(self, monkeypatch):
        monkeypatch.setattr("shared.context_providers.query_lore_multi", lambda queries: [])
        block = await LoreProvider().provide(BerriesRequest(query="hello"))
        assert block is None

    async def test_query_failure_degrades_to_no_block(self, monkeypatch):
        def boom(queries):
            raise RuntimeError("chroma down")

        monkeypatch.setattr("shared.context_providers.query_lore_multi", boom)
        block = await LoreProvider().provide(BerriesRequest(query="hello"))
        assert block is None
