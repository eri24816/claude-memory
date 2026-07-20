"""Retrieval-quality tests, including the stopword regression."""

from __future__ import annotations

import pytest

from claude_memory import db, retrieval, store

NODES = [
    {
        "title": "Move-in and key pickup",
        "summary": "Eric arrives at the Traver Heights apartment on 2026-08-05 around 10 PM.",
        "type": "todo", "about_user": True, "window_start": "2026-08-05",
    },
    {
        "title": "Ann Arbor apartment",
        "summary": "Eric's Ann Arbor apartment is at 2442 Leslie Circle, lease starting 2026-07-17.",
        "type": "fact", "about_user": True, "window_start": "2026-07-17",
    },
    {
        "title": "Fizz rebranded to Mine",
        "summary": "The student credit-builder Fizz now operates as Mine (usemine.com).",
        "type": "fact", "about_user": False, "window_start": "2026-07-20",
    },
    {
        "title": "D drive root is not writable",
        "summary": "Writing to the D drive root fails with EPERM; use a subdirectory instead.",
        "type": "code-pref",
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
        "apartment" if "apartment" in hit["summary"] else
        "fizz" if "Fizz" in hit["summary"] else
        "eperm" if "EPERM" in hit["summary"] else "other": index
        for index, hit in enumerate(hits)
    }
    assert position["apartment"] < position["fizz"]
    assert position["apartment"] < position["eperm"]


def test_exact_rare_token_wins_lexically(connection):
    hits = retrieval.search(connection, "EPERM", limit=1)
    assert "EPERM" in hits[0]["summary"]
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
            "title": f"University statement {index}",
            "summary": f"Statement about university number {index} and moving between campuses next year.",
            "type": "fact", "about_user": False, "window_start": "2026-01-01",
        }
        for index in range(40)
    ]
    store.remember(connection, world_filler)

    flat = retrieval.search(connection, "where am I moving to next month", limit=3)
    stratified = retrieval.search_stratified(connection, "where am I moving to next month")

    assert not any(hit["about_user"] for hit in flat), "precondition: flat list is starved"
    assert any(hit["about_user"] for hit in stratified)


def test_autoloaded_nodes_are_excluded_from_retrieval(connection):
    """Retrieval spends a budget the autoload set has already spent. A node that
    is verbatim in context costs a slot and reads as a second source agreeing."""
    autoloaded = retrieval.t1_ids(connection)
    assert autoloaded, "precondition: something is autoloaded"

    query = "when do I move into the apartment"
    assert any(hit["id"] in autoloaded for hit in retrieval.search(connection, query))

    hits = retrieval.search(connection, query, exclude_ids=autoloaded)
    assert not any(hit["id"] in autoloaded for hit in hits)


def test_exclusion_does_not_shrink_the_result_set(connection):
    """Regression risk: filtering after LIMIT would let excluded rows consume
    slots, silently returning fewer hits than asked for."""
    filler = [
        {"title": f"Apartment note {index}",
         "summary": f"Note number {index} about apartments and moving house.",
         "type": "fact", "about_user": False, "window_start": "2026-01-01"}
        for index in range(10)
    ]
    store.remember(connection, filler)

    excluded = retrieval.t1_ids(connection)
    hits = retrieval.search(connection, "apartment moving", limit=5,
                            exclude_ids=excluded)
    assert len(hits) == 5


@pytest.mark.parametrize("message", ["ok", "yes", "do it", "no thanks"])
def test_trivial_messages_do_not_trigger_retrieval(message):
    assert retrieval.should_retrieve(message) is False


@pytest.mark.parametrize(
    "message",
    ["where am I moving to next month?", "what are my roommates called"],
)
def test_substantive_messages_trigger_retrieval(message):
    assert retrieval.should_retrieve(message) is True
