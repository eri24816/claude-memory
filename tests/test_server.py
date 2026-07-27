"""The inspector's HTTP surface, where the store is bigger than one screen."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from claude_memory import db, server, store

PAGE = 10


@pytest.fixture
def client(tmp_path, monkeypatch):
    """A file-backed store, because each request opens and closes its own
    connection and a shared `:memory:` one dies with the first of them."""
    path = tmp_path / "memory.db"
    connection = db.connect(path)
    store.remember(connection, [
        {"claim": f"Node number {index} was written", "type": "fact",
         "about_user": True}
        for index in range(25)
    ])
    connection.close()
    monkeypatch.setattr(server, "_connection", lambda: db.connect(path))
    return TestClient(server.app)


def _page(client, offset):
    response = client.get("/api/nodes", params={"limit": PAGE, "offset": offset})
    assert response.status_code == 200
    return response.json()


def test_a_page_reports_the_total_it_was_cut_from(client):
    """Without it the tab cannot say a node was left off, so an absent node
    reads as one the store never held -- which is how the MTSA wiki nodes
    looked missing while sitting at position 604 of 629."""
    first = _page(client, 0)
    assert len(first["nodes"]) == PAGE
    assert first["total"] == 25


def test_paging_covers_every_node_exactly_once(client):
    """`updated` is identical to the second across a batch, so ordering by it
    alone lets SQLite break the tie differently per query -- skipping a row on
    one page and repeating it on the next."""
    seen = []
    for offset in range(0, 25, PAGE):
        seen.extend(node["id"] for node in _page(client, offset)["nodes"])

    assert len(seen) == 25
    assert len(set(seen)) == 25


def test_the_total_counts_the_filtered_set_not_the_table(client):
    """The page bar is a claim about the filtered view; counting the whole table
    would promise pages that the filter has nothing to put in."""
    response = client.get("/api/nodes", params={"q": "number 7", "limit": PAGE})
    body = response.json()

    assert body["total"] == 1
    assert len(body["nodes"]) == 1


def test_t1_returns_the_history_alongside_the_categorical_set(client):
    body = client.get("/api/t1").json()

    assert body["history"]
    assert "## history" in body["block"]
