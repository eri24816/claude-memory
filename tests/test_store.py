"""Invariant and round-trip tests. No LLM involved."""

from __future__ import annotations

import pytest

from claude_memory import db, retrieval, store
from claude_memory.models import InvariantError

UMICH = {
    "title": "MSCSE at University of Michigan",
    "summary": (
        "Eric is enrolled in the MSCSE program at University of Michigan EECS "
        "from 2026-08-05, with expected graduation in spring 2028."
    ),
    "type": "fact",
    "about_user": True,
    "window_start": "2026-08-05",
    "window_end": "2028-05-31",
}

CHROME_PREF = {
    "title": "Use Chrome for web research",
    "summary": "Prefer Chrome browser automation over WebSearch for web research.",
    "type": "conv-pref",
}


@pytest.fixture
def connection():
    conn = db.connect(":memory:")
    yield conn
    conn.close()


def test_insert_and_retrieve(connection):
    result = store.remember(connection, [UMICH])
    assert len(result["written"]) == 1

    hits = retrieval.search(connection, "michigan enrollment")
    assert hits, "expected the UMich node to be retrievable"
    assert hits[0]["id"] == result["written"][0]


def test_about_user_required_for_fact(connection):
    node = {**UMICH}
    node.pop("about_user")
    with pytest.raises(InvariantError, match="about_user is required"):
        store.remember(connection, [node])


def test_about_user_forbidden_for_pref(connection):
    with pytest.raises(InvariantError, match="must be null"):
        store.remember(connection, [{**CHROME_PREF, "about_user": True}])


def test_window_order_enforced(connection):
    with pytest.raises(InvariantError, match="window_start must not be after"):
        store.remember(
            connection,
            [{**UMICH, "window_start": "2028-01-01", "window_end": "2026-01-01"}],
        )


def test_idea_cannot_be_superseded(connection):
    written = store.remember(
        connection,
        [{"summary": "Try slot attention on symbolic music.", "type": "idea"}],
    )["written"][0]

    with pytest.raises(InvariantError, match="stays valid"):
        store.remember(
            connection,
            [{
                "op": "supersede",
                "supersedes": written,
                "summary": "Ran the slot attention experiment.",
                "type": "action",
                "about_user": True,
            }],
        )


def test_todo_superseded_by_action(connection):
    todo_id = store.remember(
        connection,
        [{"summary": "Get a US SSN.", "type": "todo", "about_user": True}],
    )["written"][0]

    action_id = store.remember(
        connection,
        [{
            "op": "supersede",
            "supersedes": todo_id,
            "summary": "Eric obtained a US SSN.",
            "type": "action",
            "about_user": True,
            "window_start": "2026-09-01",
        }],
    )["written"][0]

    row = connection.execute(
        "SELECT superseded_by FROM nodes WHERE id = ?", (todo_id,)
    ).fetchone()
    assert row["superseded_by"] == action_id

    # A superseded todo must drop out of both retrieval and the autoload set.
    assert all(hit["id"] != todo_id for hit in retrieval.search(connection, "SSN"))
    assert all(node["id"] != todo_id for node in retrieval.assemble_t1(connection))


def test_double_supersede_rejected(connection):
    todo_id = store.remember(
        connection, [{"summary": "Open a bank account.", "type": "todo", "about_user": True}]
    )["written"][0]
    supersede = {
        "op": "supersede",
        "supersedes": todo_id,
        "summary": "Eric opened a bank account.",
        "type": "action",
        "about_user": True,
    }
    store.remember(connection, [supersede])
    with pytest.raises(InvariantError, match="already superseded"):
        store.remember(connection, [supersede])


def test_batch_is_atomic(connection):
    with pytest.raises(InvariantError):
        store.remember(connection, [CHROME_PREF, {"summary": "bad", "type": "nonsense"}])
    assert connection.execute("SELECT COUNT(*) FROM nodes").fetchone()[0] == 0


def test_rollback_run_removes_inserts(connection):
    run = store.remember(connection, [UMICH, CHROME_PREF])
    assert connection.execute("SELECT COUNT(*) FROM nodes").fetchone()[0] == 2

    store.rollback_run(connection, run["capture_run_id"])
    assert connection.execute("SELECT COUNT(*) FROM nodes").fetchone()[0] == 0
    assert connection.execute("SELECT COUNT(*) FROM nodes_fts").fetchone()[0] == 0


def test_t1_excludes_world_facts(connection):
    store.remember(
        connection,
        [
            UMICH,
            {
                "summary": "The credit builder Fizz now operates as Mine.",
                "type": "fact",
                "about_user": False,
                "window_start": "2026-07-20",
            },
        ],
    )
    summaries = [node["summary"] for node in retrieval.assemble_t1(connection)]
    assert any("MSCSE" in summary for summary in summaries)
    assert not any("Fizz" in summary for summary in summaries)


def test_t1_respects_lead_time(connection):
    store.remember(
        connection,
        [{
            "summary": "Eric will start a postdoc.",
            "type": "fact",
            "about_user": True,
            "window_start": "2030-01-01",
        }],
    )
    assert retrieval.assemble_t1(connection) == []


def test_scope_isolation(connection):
    store.remember(
        connection,
        [{
            "summary": "The dev server is started with run.bat.",
            "type": "fact",
            "about_user": True,
            "scope": "project:polygenie",
        }],
    )
    assert retrieval.assemble_t1(connection, scope="project:other") == []
    assert len(retrieval.assemble_t1(connection, scope="project:polygenie")) == 1


def test_edges_require_existing_target(connection):
    with pytest.raises(InvariantError, match="no such node"):
        store.remember(
            connection,
            [{
                "summary": "Eric plans to apply slot attention to music.",
                "type": "intention",
                "about_user": True,
                "edges": [{"rel": "motivates", "dst": "does-not-exist"}],
            }],
        )
