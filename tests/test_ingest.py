"""Ingest produces raw nodes, and refining them survives re-ingest."""

from __future__ import annotations

import pytest

from claude_memory import db, ingest, retrieval, store

PAGE = """\
# Espresso

## Grind size

A finer grind slows the flow and raises extraction, so a sour shot usually wants
a finer setting rather than a longer one. Grind is the fastest lever available.

## Water temperature

Lighter roasts extract poorly below 94C. Darker roasts scorch above it, which is
why a single fixed temperature suits neither end of the range.
"""


@pytest.fixture
def workspace(tmp_path):
    connection = db.connect(":memory:")
    page = tmp_path / "espresso.md"
    page.write_text(PAGE, encoding="utf-8")
    yield connection, page
    connection.close()


def test_sections_are_raw_not_facts(workspace):
    """Regression: sections were typed 'fact', about_user=False. A prose chunk
    asserts nothing in particular, and calling it a fact put unread text on
    equal footing with claims something had actually judged."""
    connection, page = workspace
    ingest.ingest_path(connection, page)

    rows = connection.execute("SELECT type, about_user FROM nodes").fetchall()
    assert rows
    assert all(row["type"] == "raw" for row in rows)
    assert all(row["about_user"] is None for row in rows)


def test_raw_still_reachable_by_search(workspace):
    """Raw nodes carry no about_user, so a `= 0` world filter would hide them
    from stratified retrieval entirely."""
    connection, page = workspace
    ingest.ingest_path(connection, page)

    hits = retrieval.search_stratified(connection, "why is my espresso shot sour")
    assert any(hit["type"] == "raw" for hit in hits)


def test_refined_node_outranks_the_raw_chunk(workspace):
    connection, page = workspace
    ingest.ingest_path(connection, page)
    raw_id = connection.execute(
        "SELECT id FROM nodes WHERE title LIKE '%Grind size%'"
    ).fetchone()["id"]

    store.remember(connection, [{
        "op": "supersede", "supersedes": raw_id, "title": "Grind size and extraction",
        "summary": "A finer espresso grind slows flow and raises extraction, so a "
                   "sour shot calls for a finer setting before a longer one.",
        "type": "fact", "about_user": False,
    }])

    hits = retrieval.search(connection, "sour espresso shot grind", limit=5)
    assert hits[0]["type"] == "fact"
    assert not any(hit["id"] == raw_id for hit in hits), "superseded chunk still shown"


def test_reingest_keeps_refinements_of_surviving_sections(workspace):
    """Re-ingest deletes and rewrites a file's derived nodes. Without carrying
    the supersession pointers across, an unrelated edit elsewhere in the page
    would resurrect a refined chunk and silently undo the refinement."""
    connection, page = workspace
    ingest.ingest_path(connection, page)
    raw_id = connection.execute(
        "SELECT id FROM nodes WHERE title LIKE '%Grind size%'"
    ).fetchone()["id"]
    store.remember(connection, [{
        "op": "supersede", "supersedes": raw_id, "title": "Grind size and extraction",
        "summary": "Finer grind, higher extraction.", "type": "fact",
        "about_user": False,
    }])

    page.write_text(PAGE + "\n## Basket size\n\nAn 18g basket is the common "
                    "default, and dosing far under it channels badly.\n",
                    encoding="utf-8")
    stats = ingest.ingest_path(connection, page)

    assert stats["refinements_kept"] == 1
    assert stats["refinements_lost"] == 0
    assert connection.execute(
        "SELECT superseded_by FROM nodes WHERE id = ?", (raw_id,)
    ).fetchone()["superseded_by"] is not None
