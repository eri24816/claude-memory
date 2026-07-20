"""The literal text an agent receives: T1, retrieved context, and a dig."""

from __future__ import annotations

import pytest

from claude_memory import db, retrieval, store

NODES = [
    {"title": "How this memory works", "type": "meta",
     "summary": "Retrieved memories arrive as type, date, title, content. Dig by title."},
    {"title": "Ann Arbor apartment", "type": "fact", "about_user": True,
     "summary": "Eric's Ann Arbor apartment is at 2442 Leslie Circle.",
     "window_start": "2026-07-17", "window_end": "2027-07-31"},
    {"title": "Move-in and key pickup", "type": "todo", "about_user": True,
     "summary": "Eric arrives at the apartment on 2026-08-05 around 10 PM.",
     "window_start": "2026-08-05", "window_end": "2026-08-05"},
]


@pytest.fixture
def connection():
    conn = db.connect(":memory:")
    store.remember(conn, NODES)
    yield conn
    conn.close()


def test_meta_leads_the_autoload_block(connection):
    """meta is the instructions for reading everything under it; a reader who
    meets the facts first has already read them wrong."""
    block = retrieval.render_t1(retrieval.assemble_t1(connection))
    assert block.index("## meta") < block.index("## fact")


def test_context_rows_are_type_date_title_content(connection):
    hits = retrieval.search(connection, "apartment address", limit=1)
    block = retrieval.render_context(hits)

    assert block.startswith("# memory that could be useful:")
    row = block.splitlines()[1]
    kind, date, title, content = (part.strip() for part in row.split(",", 3))
    assert kind == "fact"
    assert date == "2026-07-17..2027-07-31"
    assert title == "Ann Arbor apartment"
    assert "Leslie Circle" in content


def test_context_carries_no_standing_instructions(connection):
    """Guidance is a per-message tax when it is injected per message. The rules
    for reading these rows -- including what `raw` means -- live in meta."""
    block = retrieval.render_context(retrieval.search(connection, "apartment"))
    assert "raw" not in block.lower()
    assert "supersede" not in block.lower()


def test_titles_are_unique_and_the_newcomer_yields(connection):
    store.remember(connection, [
        {"title": "Ann Arbor apartment", "type": "fact", "about_user": False,
         "summary": "A different node that happens to want the same name."},
    ])
    titles = [row["title"] for row in connection.execute(
        "SELECT title FROM nodes WHERE title LIKE 'Ann Arbor apartment%'"
    )]
    assert sorted(titles) == ["Ann Arbor apartment", "Ann Arbor apartment (2)"]


def test_supersession_transfers_the_title_to_the_successor(connection):
    """A title names a concept, not a version, so it must resolve to whatever is
    currently true. Letting the newcomer take the suffix would push the live node
    away from its natural name and leave the bare title on something dead."""
    store.remember(connection, [
        {"op": "supersede", "supersedes": "ann-arbor-apartment",
         "title": "Ann Arbor apartment", "type": "fact", "about_user": True,
         "summary": "Eric's apartment is at 2442 Leslie Circle, lease renewed."},
    ])
    current = retrieval.dig(connection, "Ann Arbor apartment")
    assert "renewed" in current["summary"]
    assert current["stale"] is False
    assert current["supersedes"] == ["Ann Arbor apartment (2)"]

    superseded = retrieval.dig(connection, "Ann Arbor apartment (2)")
    assert superseded["superseded_by"] == "Ann Arbor apartment"


def test_write_operations_accept_titles(connection):
    """Everything an agent is shown carries a title and never an id, so a handle
    it hands back is a title. Requiring ids made supersede and stale unreachable
    from the only surface that uses them."""
    store.remember(connection, [
        {"op": "supersede", "supersedes": "Move-in and key pickup",
         "title": "Move-in and key pickup", "type": "action", "about_user": True,
         "summary": "Eric moved into the apartment on 2026-08-05.",
         "window_start": "2026-08-05"},
    ])
    assert retrieval.dig(connection, "Move-in and key pickup")["type"] == "action"

    store.set_stale(connection, "Ann Arbor apartment")
    assert retrieval.dig(connection, "Ann Arbor apartment")["stale"] is True


def test_unknown_handle_is_reported_by_name(connection):
    with pytest.raises(Exception, match="No such memory|no node titled"):
        store.set_stale(connection, "No such memory")


def test_dig_resolves_links_to_titles(connection):
    node = retrieval.dig(connection, "Move-in and key pickup")
    assert node["type"] == "todo"
    assert node["about_user"] is True
    assert node["window_start"] == "2026-08-05"

    block = retrieval.render_dig(node)
    assert block.startswith("# Move-in and key pickup")
    assert "type: todo" in block
    assert "time: 2026-08-05" in block


def test_dig_misses_report_the_title(connection):
    assert retrieval.dig(connection, "No such memory") is None
    assert "No such memory" in retrieval.render_dig(None, "No such memory")
