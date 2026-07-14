"""
tests/test_lore.py

Tests for the lore file parser in scripts/reindex_lore.py.
"""

from pathlib import Path

from scripts.reindex_lore import parse_lore_file


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
