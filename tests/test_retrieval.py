"""Retrieval-quality tests, including the stopword regression."""

from __future__ import annotations

import pytest

from claude_memory import db, retrieval, store

NODES = [
    {
        "claim": "Eric moves into Traver Heights apartment",
        "detail": "Arrives 2026-08-05 around 10 PM for key pickup.",
        "type": "todo", "about_user": True, "window_start": "2026-08-05",
    },
    {
        "claim": "Eric's apartment is 2442 Leslie Circle",
        "detail": "Ann Arbor, lease starting 2026-07-17.",
        "type": "fact", "about_user": True, "window_start": "2026-07-17",
    },
    {
        "claim": "Fizz rebranded to Mine",
        "detail": "The student credit-builder now operates as Mine (usemine.com).",
        "type": "fact", "about_user": False, "window_start": "2026-07-20",
    },
    {
        # Detail carries the rare token, so this doubles as the check that
        # indexing reaches past the claim -- compression is display-only.
        "claim": "D drive root is not writable",
        "detail": "Writing to the D drive root fails with EPERM; use a subdirectory.",
        "type": "fact", "about_user": False, "window_start": "2026-06-01",
    },
]


@pytest.fixture
def connection():
    conn = db.connect(":memory:")
    store.remember(conn, NODES)
    yield conn
    conn.close()


def test_stopwords_do_not_poison_ranking(connection):
    """Regression: OR-ing every token let BM25 match "to"/"month"/"I" and hand
    irrelevant nodes rank 1, outranking the correct semantic hits.

    Asserts relative order rather than absence: on a tiny corpus every node
    comes back, so what matters is that the relevant ones rank above the noise.
    """
    hits = retrieval.search(connection, "where am I moving to next month?")
    position = {
        "apartment" if "Leslie" in hit["claim"] else
        "fizz" if "Fizz" in hit["claim"] else
        "eperm" if "D drive" in hit["claim"] else "other": index
        for index, hit in enumerate(hits)
    }
    assert position["apartment"] < position["fizz"]
    assert position["apartment"] < position["eperm"]


def test_exact_rare_token_wins_lexically(connection):
    hits = retrieval.search(connection, "EPERM", limit=1)
    assert "EPERM" in hits[0]["detail"]
    assert hits[0]["lexical_rank"] == 1


def test_component_ranks_are_reported(connection):
    hits = retrieval.search(connection, "apartment lease", limit=3)
    assert any(hit["lexical_rank"] for hit in hits)
    assert any(hit["semantic_rank"] for hit in hits)


def test_stratified_guarantees_personal_slots(connection):
    """Regression: a large world corpus starved the personal nodes entirely.

    With ~500 wiki nodes against six personal ones, a flat ranked list returned
    only wiki sections for "where am I moving to next month". Stratifying gives
    each population its own slots.
    """
    world_filler = [
        {
            "claim": f"University statement number {index}",
            "detail": f"Statement about university {index} and moving between campuses next year.",
            "type": "fact", "about_user": False, "window_start": "2026-01-01",
        }
        for index in range(40)
    ]
    store.remember(connection, world_filler)

    flat = retrieval.search(connection, "where am I moving to next month", limit=3)
    stratified = retrieval.search_stratified(connection, "where am I moving to next month")

    assert not any(hit["about_user"] for hit in flat), "precondition: flat list is starved"
    assert any(hit["about_user"] for hit in stratified)


def test_autoloaded_nodes_are_retrievable(connection):
    """Retrieval used to subtract the autoload set. It no longer does: a node
    matching the query is the answer whether or not it is also in context, and
    silently withholding the best hit because it was loaded earlier means a
    search for something present returns everything except it."""
    autoloaded = {node["id"] for node in retrieval.assemble_t1(connection)}
    assert autoloaded, "precondition: something is autoloaded"

    hits = retrieval.search(connection, "when do I move into the apartment")
    assert any(hit["id"] in autoloaded for hit in hits)


def test_a_search_returns_as_many_hits_as_asked_for(connection):
    filler = [
        {"claim": f"Apartment note number {index}",
         "detail": f"Note {index} about apartments and moving house.",
         "type": "fact", "about_user": False, "window_start": "2026-01-01"}
        for index in range(10)
    ]
    store.remember(connection, filler)

    assert len(retrieval.search(connection, "apartment moving", limit=5)) == 5


@pytest.mark.parametrize("message", ["ok", "yes", "do it", "no thanks"])
def test_trivial_messages_do_not_trigger_retrieval(message):
    assert retrieval.should_retrieve(message) is False


@pytest.mark.parametrize(
    "message",
    ["where am I moving to next month?", "what are my roommates called"],
)
def test_substantive_messages_trigger_retrieval(message):
    assert retrieval.should_retrieve(message) is True
