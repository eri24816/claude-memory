"""Carrying a pre-0.1.0 store forward.

The invariant worth more than all the others: the old store is never written.
Everything else here can be retried; a migration that damages the only copy of
someone's memory cannot.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from claude_memory import db, migration, settings

V0_SCHEMA = """
CREATE TABLE nodes (
    id TEXT PRIMARY KEY, title TEXT, summary TEXT NOT NULL, type TEXT NOT NULL,
    about_user INTEGER, scope TEXT NOT NULL DEFAULT 'global',
    window_start TEXT, window_end TEXT, stale INTEGER NOT NULL DEFAULT 0,
    superseded_by TEXT REFERENCES nodes(id), origin TEXT NOT NULL DEFAULT 'original',
    parent TEXT REFERENCES nodes(id), derived_from TEXT, content_hash TEXT,
    locator TEXT, source_session TEXT, updated TEXT NOT NULL
);
"""

V0_NODES = [
    ("apartment", "Ann Arbor apartment",
     "Eric's Ann Arbor apartment is at 2442 Leslie Circle, a long sentence that "
     "was contracted to be short and was not.",
     "fact", 1, "original", None, None),
    ("umich", "MSCSE at Michigan", "Eric is enrolled in MSCSE from 2026-08-05.",
     "fact", 1, "original", None, None),
    ("chrome", "Use Chrome for research", "Prefer Chrome over WebSearch.",
     "conv-pref", None, "original", None, None),
    ("tabs", "No tabs in Python", "Never indent Python with tabs.",
     "code-pref", None, "original", None, None),
    ("howto", "How this memory works", "Dig by title. Refine raw on demand.",
     "meta", None, "original", None, None),
    ("wiki-1", "Wiki > Section", "A chunk of unread wiki prose.",
     "raw", None, "derived", "wiki/page.md", None),
    ("old", "Superseded fact", "Something that was replaced.",
     "fact", 1, "original", None, "apartment"),
]


@pytest.fixture
def legacy(tmp_path, monkeypatch):
    """A pre-0.1.0 store on disk, with the new store pointed somewhere fresh."""
    path = tmp_path / "legacy" / "memory.db"
    path.parent.mkdir(parents=True, exist_ok=True)

    raw = sqlite3.connect(str(path))
    raw.executescript(V0_SCHEMA)
    raw.executemany(
        "INSERT INTO nodes (id, title, summary, type, about_user, origin, "
        "derived_from, superseded_by, updated) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, '2026-07-20')",
        V0_NODES,
    )
    raw.commit()
    raw.close()

    monkeypatch.setattr(db, "LEGACY_DB_PATH", path)
    monkeypatch.setattr(db, "DEFAULT_DB_PATH", tmp_path / "settings" / "memory.db")
    return path


def test_creating_a_store_flags_a_pending_migration(legacy):
    """Step 0-1: the trigger is store *creation*, not a version comparison. A
    version check has to open the old store as though it were current, which is
    what made the previous design fragile."""
    assert not migration.is_migrating()

    connection = db.connect(db.DEFAULT_DB_PATH)
    connection.close()

    assert migration.is_migrating()
    state = migration.read_state()
    assert state["legacy_db"] == str(legacy)
    assert state["legacy_total"] == 5  # 2 facts + 2 prefs + meta; raw and superseded excluded
    assert state["legacy_raw_skipped"] == 1


def test_no_legacy_store_means_no_migration(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "LEGACY_DB_PATH", tmp_path / "nothing" / "memory.db")
    connection = db.connect(tmp_path / "fresh.db")
    connection.close()
    assert not migration.is_migrating()


def test_the_old_store_is_opened_read_only(legacy):
    """The one invariant that matters. Enforced by the connection's mode rather
    than by everyone remembering not to write."""
    connection = migration._open_legacy(legacy)
    try:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            connection.execute("DELETE FROM nodes")
    finally:
        connection.close()


def test_list_skips_raw_and_superseded(legacy):
    """Raw chunks come back from the wiki in 0.2.0. Superseded nodes are history
    the new store cannot express without also carrying their successors and the
    pointers between them."""
    ids = {node["id"] for node in migration.list_nodes(limit=100, path=legacy)}
    assert ids == {"apartment", "umich", "chrome", "tabs", "howto"}


def test_list_gives_the_agent_what_it_needs_to_rewrite(legacy):
    node = next(n for n in migration.list_nodes(limit=100, path=legacy)
                if n["id"] == "apartment")
    assert node["title"] and node["summary"]
    assert node["type"] == "fact"
    assert node["about_user"] == 1


def test_list_paginates(legacy):
    first = migration.list_nodes(limit=2, offset=0, path=legacy)
    second = migration.list_nodes(limit=2, offset=2, path=legacy)
    assert len(first) == len(second) == 2
    assert {n["id"] for n in first} & {n["id"] for n in second} == set()


def test_progress_is_the_new_store_itself(legacy):
    """No cursor to keep in sync: an interrupted migration resumes by looking at
    what is already there."""
    from claude_memory import store

    connection = db.connect(db.DEFAULT_DB_PATH)
    assert migration.status(connection)["written_so_far"] == 0

    store.remember(connection, [
        {"claim": "Eric's apartment is 2442 Leslie Circle", "type": "fact",
         "about_user": True, "window_start": "2026-07-17"},
    ])
    assert migration.status(connection)["written_so_far"] == 1
    connection.close()


def test_done_clears_the_flag_and_keeps_the_old_store(legacy):
    """Never deleted: it is the only copy of anything the agent chose not to
    carry, and deleting a user's memory is not this code's decision."""
    db.connect(db.DEFAULT_DB_PATH).close()
    assert migration.is_migrating()

    result = migration.done()

    assert result["migrating"] is False
    assert not migration.is_migrating()
    assert legacy.exists()
    assert sqlite3.connect(str(legacy)).execute(
        "SELECT COUNT(*) FROM nodes"
    ).fetchone()[0] == len(V0_NODES)


def test_a_stale_pid_is_not_killed(legacy, monkeypatch):
    """The discovery file outlives the process that wrote it -- nothing removes
    it on a crash or a reboot -- and operating systems reuse pids. Trusting it
    blindly means killing whatever unrelated program now holds that number."""
    (legacy.parent / "daemon.json").write_text(
        json.dumps({"port": 1234, "pid": 999999}), encoding="utf-8"
    )
    killed = []
    monkeypatch.setattr(migration, "_is_daemon_process", lambda pid: False)
    monkeypatch.setattr("claude_memory.daemon._terminate",
                        lambda pid: killed.append(pid))

    assert migration._stop_legacy_daemon() is None
    assert not killed, "must never terminate a pid it could not identify"


def test_a_verified_daemon_is_stopped(legacy, monkeypatch):
    """Step 2: the old daemon answers from the old store and holds the code it
    imported at start-up, so it has to go."""
    (legacy.parent / "daemon.json").write_text(
        json.dumps({"port": 1234, "pid": 4321}), encoding="utf-8"
    )
    killed = []
    monkeypatch.setattr(migration, "_is_daemon_process", lambda pid: True)
    monkeypatch.setattr("claude_memory.daemon._terminate",
                        lambda pid: killed.append(pid))

    assert migration._stop_legacy_daemon() == 4321
    assert killed == [4321]


def test_hooks_announce_the_pending_migration(legacy):
    from claude_memory import hooks

    connection = db.connect(db.DEFAULT_DB_PATH)
    try:
        start = hooks.build_session_start_context({"cwd": "/tmp"}, connection)
        prompt = hooks.build_user_prompt_context({"prompt": "ok"}, connection)
        for block in (start, prompt):
            assert "MID-MIGRATION" in block
            assert str(legacy) in block
    finally:
        connection.close()


def test_hooks_go_quiet_once_done(legacy):
    from claude_memory import hooks

    connection = db.connect(db.DEFAULT_DB_PATH)
    try:
        migration.done()
        assert hooks.migration_notice(connection) == ""
    finally:
        connection.close()


def test_a_legacy_store_as_the_target_is_reported_not_converted(legacy):
    """Someone pointing CLAUDE_MEMORY_DB straight at the old store. Converting it
    in place is exactly what 0.1.0 stopped doing, so say where to point instead."""
    from claude_memory import hooks

    connection = db.connect(legacy)
    try:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(nodes)")}
        assert "summary" in columns, "the old store must be left untouched"
        assert "MISCONFIGURED" in hooks.migration_notice(connection)
    finally:
        connection.close()
