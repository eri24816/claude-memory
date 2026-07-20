"""Invariant and round-trip tests. No LLM involved."""

from __future__ import annotations

import json

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
        [{"title": "Slot attention on symbolic music",
          "summary": "Try slot attention on symbolic music.", "type": "idea"}],
    )["written"][0]

    with pytest.raises(InvariantError, match="stays valid"):
        store.remember(
            connection,
            [{
                "op": "supersede",
                "supersedes": written,
                "title": "Ran the slot attention experiment",
                "summary": "Ran the slot attention experiment.",
                "type": "action",
                "about_user": True,
            }],
        )


def test_todo_superseded_by_action(connection):
    todo_id = store.remember(
        connection,
        [{"title": "Get a US SSN", "summary": "Get a US SSN.",
          "type": "todo", "about_user": True}],
    )["written"][0]

    action_id = store.remember(
        connection,
        [{
            "op": "supersede",
            "supersedes": todo_id,
            "title": "Eric obtained a US SSN",
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
        connection, [{"title": "Open a bank account",
                      "summary": "Open a bank account.", "type": "todo",
                      "about_user": True}]
    )["written"][0]
    supersede = {
        "op": "supersede",
        "supersedes": todo_id,
        "title": "Eric opened a bank account",
        "summary": "Eric opened a bank account.",
        "type": "action",
        "about_user": True,
    }
    store.remember(connection, [supersede])
    with pytest.raises(InvariantError, match="already superseded"):
        store.remember(connection, [supersede])


def test_batch_is_atomic(connection):
    with pytest.raises(InvariantError):
        store.remember(connection, [CHROME_PREF,
                                    {"title": "bad", "summary": "bad", "type": "nonsense"}])
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
                "title": "Fizz rebranded to Mine",
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
            "title": "Future postdoc",
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
            "title": "Dev server launch command",
            "summary": "The dev server is started with run.bat.",
            "type": "fact",
            "about_user": True,
            "scope": "project:polygenie",
        }],
    )
    assert retrieval.assemble_t1(connection, scope="project:other") == []
    assert len(retrieval.assemble_t1(connection, scope="project:polygenie")) == 1


def test_source_session_defaults_from_the_live_session(connection, monkeypatch):
    """Regression: nothing ever set source_session automatically, so every
    node in the store had it NULL -- the field existed but the agent had no
    reason to type its own session id by hand on every write."""
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "live-session-abc")
    written = store.remember(connection, [UMICH])["written"][0]
    row = connection.execute(
        "SELECT source_session FROM nodes WHERE id = ?", (written,)
    ).fetchone()
    assert row["source_session"] == "live-session-abc"


def test_source_session_explicit_value_wins(connection, monkeypatch):
    """A backfill or an ingest describing a different session's content must
    not be overridden by whichever session happens to run the write."""
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "live-session-abc")
    written = store.remember(
        connection, [{**UMICH, "source_session": "original-session-xyz"}]
    )["written"][0]
    row = connection.execute(
        "SELECT source_session FROM nodes WHERE id = ?", (written,)
    ).fetchone()
    assert row["source_session"] == "original-session-xyz"


def test_source_session_not_defaulted_for_derived_nodes(connection, monkeypatch):
    """A derived (ingested) node's provenance is the file it came from, not
    whichever session happened to run the ingest command -- stamping one in
    would misattribute wiki content to a conversation that never produced it."""
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "live-session-abc")
    written = store.remember(connection, [{
        "title": "Ingested wiki section", "summary": "Some wiki content.",
        "type": "raw", "origin": "derived", "derived_from": "wiki/page.md",
    }])["written"][0]
    row = connection.execute(
        "SELECT source_session FROM nodes WHERE id = ?", (written,)
    ).fetchone()
    assert row["source_session"] is None


def test_source_session_null_without_the_env_var(connection, monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    written = store.remember(connection, [UMICH])["written"][0]
    row = connection.execute(
        "SELECT source_session FROM nodes WHERE id = ?", (written,)
    ).fetchone()
    assert row["source_session"] is None


def _write_transcript(directory, session_id, events):
    """A minimal fake transcript: one JSON object per line, newest last."""
    path = directory / f"{session_id}.jsonl"
    path.write_text(
        "\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8"
    )
    return path


@pytest.fixture
def transcripts_dir(tmp_path, monkeypatch):
    directory = tmp_path / "projects"
    directory.mkdir()
    monkeypatch.setenv("CLAUDE_MEMORY_TRANSCRIPTS_DIR", str(directory))
    store._live_transcript_uuid.cache_clear()
    yield directory
    store._live_transcript_uuid.cache_clear()


def test_locator_defaults_to_the_most_recent_user_turn(connection, monkeypatch, transcripts_dir):
    """Regression target: the whole point is zero effort for the normal case --
    a live capture about the conversation that is currently producing it."""
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "live-session")
    _write_transcript(transcripts_dir, "live-session", [
        {"uuid": "u1", "message": {"role": "user", "content": "first question"}},
        {"uuid": "a1", "message": {"role": "assistant", "content": [{"type": "text", "text": "answer"}]}},
        {"uuid": "u2", "message": {"role": "user", "content": "second question"}},
        {"uuid": "a2", "message": {"role": "assistant", "content": [{"type": "text", "text": "answer"}]}},
    ])

    written = store.remember(connection, [UMICH])["written"][0]
    row = connection.execute(
        "SELECT locator FROM nodes WHERE id = ?", (written,)
    ).fetchone()
    assert row["locator"] == "u2"


def test_locator_skips_a_trailing_tool_result_turn(connection, monkeypatch, transcripts_dir):
    """Regression: caught live against this project's own real transcript. A
    tool_result is sent back to the API as a role=user turn, so "most recent
    user-role event" is very often the harness's own tool output, not the last
    thing Eric actually typed -- landing a locator there is far less useful."""
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "live-session")
    _write_transcript(transcripts_dir, "live-session", [
        {"uuid": "u1", "message": {"role": "user", "content": "what Eric actually asked"}},
        {"uuid": "a1", "message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": "t1", "name": "Bash", "input": {}},
        ]}},
        {"uuid": "u2", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": "command output"},
        ]}},
    ])

    written = store.remember(connection, [UMICH])["written"][0]
    row = connection.execute(
        "SELECT locator FROM nodes WHERE id = ?", (written,)
    ).fetchone()
    assert row["locator"] == "u1"


def test_locator_not_defaulted_when_source_session_points_elsewhere(
    connection, monkeypatch, transcripts_dir
):
    """The exact bug this guard exists to prevent: a backfill naming a
    different session's content must not get "wherever this process happens
    to be right now" stamped on as its locator -- that would point at the
    wrong transcript entirely."""
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "live-session")
    _write_transcript(transcripts_dir, "live-session", [
        {"uuid": "u1", "message": {"role": "user"}},
    ])

    written = store.remember(
        connection, [{**UMICH, "source_session": "some-other-session"}]
    )["written"][0]
    row = connection.execute(
        "SELECT locator, source_session FROM nodes WHERE id = ?", (written,)
    ).fetchone()
    assert row["source_session"] == "some-other-session"
    assert row["locator"] is None


def test_locator_explicit_value_wins(connection, monkeypatch, transcripts_dir):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "live-session")
    _write_transcript(transcripts_dir, "live-session", [
        {"uuid": "u1", "message": {"role": "user"}},
    ])

    written = store.remember(
        connection, [{**UMICH, "locator": "already-known-uuid"}]
    )["written"][0]
    row = connection.execute(
        "SELECT locator FROM nodes WHERE id = ?", (written,)
    ).fetchone()
    assert row["locator"] == "already-known-uuid"


def test_locator_not_defaulted_for_derived_nodes(connection, monkeypatch, transcripts_dir):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "live-session")
    _write_transcript(transcripts_dir, "live-session", [
        {"uuid": "u1", "message": {"role": "user"}},
    ])

    written = store.remember(connection, [{
        "title": "Ingested wiki section", "summary": "Some wiki content.",
        "type": "raw", "origin": "derived", "derived_from": "wiki/page.md",
    }])["written"][0]
    row = connection.execute(
        "SELECT locator FROM nodes WHERE id = ?", (written,)
    ).fetchone()
    assert row["locator"] is None


def test_locator_gracefully_absent_without_a_matching_transcript(
    connection, monkeypatch, transcripts_dir
):
    """No transcript file for this session id -- e.g. a hook-driven write in
    a test harness, or a transcript that has not synced to disk yet."""
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "session-with-no-file")
    written = store.remember(connection, [UMICH])["written"][0]
    row = connection.execute(
        "SELECT locator FROM nodes WHERE id = ?", (written,)
    ).fetchone()
    assert row["locator"] is None


def test_edges_require_existing_target(connection):
    with pytest.raises(InvariantError, match="no such node"):
        store.remember(
            connection,
            [{
                "title": "Apply slot attention to music",
                "summary": "Eric plans to apply slot attention to music.",
                "type": "intention",
                "about_user": True,
                "edges": [{"rel": "motivates", "dst": "does-not-exist"}],
            }],
        )
