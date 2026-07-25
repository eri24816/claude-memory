"""v0 -> v1 migration.

The one piece of this codebase that runs against data it did not create, on
machines the author cannot inspect, exactly once. Everything else can be fixed
in the next release; a migration that eats someone's store cannot.
"""

from __future__ import annotations

import sqlite3

import pytest

from claude_memory import db, migrate, settings
from claude_memory.models import InvariantError

V0_SCHEMA = """
CREATE TABLE nodes (
    id TEXT PRIMARY KEY, title TEXT, summary TEXT NOT NULL, type TEXT NOT NULL,
    about_user INTEGER, scope TEXT NOT NULL DEFAULT 'global',
    window_start TEXT, window_end TEXT, stale INTEGER NOT NULL DEFAULT 0,
    superseded_by TEXT REFERENCES nodes(id), origin TEXT NOT NULL DEFAULT 'original',
    parent TEXT REFERENCES nodes(id), derived_from TEXT, content_hash TEXT,
    locator TEXT, source_session TEXT, updated TEXT NOT NULL
);
CREATE UNIQUE INDEX nodes_title ON nodes(title);
CREATE TABLE node_revisions (
    node_id TEXT NOT NULL, revision INTEGER NOT NULL, op TEXT NOT NULL,
    summary TEXT, capture_run_id TEXT, recorded_at TEXT NOT NULL,
    PRIMARY KEY (node_id, revision)
);
CREATE TABLE node_edges (
    src_id TEXT NOT NULL REFERENCES nodes(id),
    dst_id TEXT NOT NULL REFERENCES nodes(id),
    rel TEXT NOT NULL, PRIMARY KEY (src_id, dst_id, rel)
);
CREATE VIRTUAL TABLE nodes_fts USING fts5(title, summary, keywords);
CREATE VIRTUAL TABLE nodes_vec USING vec0(embedding float[384]);
CREATE TABLE hook_state (
    session_id TEXT PRIMARY KEY, stop_count INTEGER NOT NULL DEFAULT 0,
    updated TEXT NOT NULL
);
"""

V0_NODES = [
    ("apartment", "Ann Arbor apartment",
     "Eric's Ann Arbor apartment is at 2442 Leslie Circle, a long sentence that "
     "was contracted to be short and was not.", "fact", 1, "original", None),
    ("umich", "MSCSE at Michigan",
     "Eric is enrolled in MSCSE from 2026-08-05.", "fact", 1, "original", None),
    ("chrome", "Use Chrome for research",
     "Prefer Chrome automation over WebSearch.", "conv-pref", None, "original", None),
    ("tabs", "No tabs in Python",
     "Never indent Python with tabs.", "code-pref", None, "original", None),
    ("howto", "How this memory works",
     "Dig by title. Raw chunks are unread source text; refine them on demand.",
     "meta", None, "original", None),
    ("wiki-1", "Wiki > Section",
     "A chunk of unread wiki prose.", "raw", None, "derived", "wiki/page.md"),
    ("wiki-2", "Wiki > Other",
     "Another chunk.", "raw", None, "derived", "wiki/page.md"),
]


@pytest.fixture
def v0_store(tmp_path):
    """A store shaped exactly like v0, on disk, with no version stamp."""
    import sqlite_vec

    path = tmp_path / "memory.db"
    raw = sqlite3.connect(str(path))
    raw.enable_load_extension(True)
    sqlite_vec.load(raw)  # the v0 schema declares a vec0 virtual table
    raw.enable_load_extension(False)
    raw.executescript(V0_SCHEMA)
    raw.executemany(
        "INSERT INTO nodes (id, title, summary, type, about_user, origin, "
        "derived_from, updated) VALUES (?, ?, ?, ?, ?, ?, ?, '2026-07-20')",
        V0_NODES,
    )
    raw.commit()
    raw.close()
    return path


def _connect(path):
    connection = db.connect(path)
    return connection


def test_v0_store_is_recognised_as_needing_migration(v0_store):
    """Every statement in schema.sql is IF NOT EXISTS, so connecting to a v0
    store silently changes nothing. The version stamp is the only reliable
    signal, and it must not be written as a side effect of connecting."""
    connection = _connect(v0_store)
    assert db.needs_migration(connection)
    assert db.user_version(connection) == 0
    assert "summary" in {row[1] for row in connection.execute("PRAGMA table_info(nodes)")}


def test_fresh_store_is_stamped_without_migrating(tmp_path):
    connection = db.connect(tmp_path / "new.db")
    assert not db.needs_migration(connection)


def test_prefs_become_files_and_leave_the_store(v0_store):
    connection = _connect(v0_store)
    migrate.run(connection, v0_store)

    assert "Chrome automation" in settings.read("conv")
    assert "tabs" in settings.read("code")
    remaining = {row[0] for row in connection.execute("SELECT type FROM nodes")}
    assert not remaining & {"conv-pref", "code-pref", "meta"}


def test_meta_is_replaced_not_migrated(v0_store):
    """A v0 meta node documents titles, summaries and refinement-on-demand --
    none of which exist in v1. Dumping it verbatim would preload actively wrong
    instructions into every session."""
    connection = _connect(v0_store)
    migrate.run(connection, v0_store)

    meta = settings.read("meta")
    assert "Dig by title" not in meta
    assert "claim" in meta


def test_raw_nodes_are_dropped(v0_store):
    connection = _connect(v0_store)
    report = migrate.run(connection, v0_store)

    assert report["raw_dropped"] == 2
    assert connection.execute(
        "SELECT COUNT(*) FROM nodes WHERE origin = 'derived'"
    ).fetchone()[0] == 0


def test_claims_are_pending_and_title_survives_until_they_are_written(v0_store):
    """The agent needs the old title and summary to write a good claim, so title
    outlives the schema change; `claim IS NULL` is the migration's whole state."""
    connection = _connect(v0_store)
    report = migrate.run(connection, v0_store)

    assert report["pending"] == 2
    assert db.needs_migration(connection)

    batch = migrate.next_batch(connection, 10)
    assert {row["id"] for row in batch} == {"apartment", "umich"}
    assert batch[0]["title"] and batch[0]["detail"]


def test_cursor_is_resumable_and_finalizes_at_zero(v0_store):
    """An agent can run out of context mid-migration. Resuming must be the
    default behaviour, not a recovery procedure."""
    connection = _connect(v0_store)
    migrate.run(connection, v0_store)

    migrate.set_claims(connection, [
        {"id": "apartment", "claim": "Eric's apartment is 2442 Leslie Circle"},
    ])
    assert migrate.status(connection)["pending"] == 1
    assert db.needs_migration(connection)

    result = migrate.set_claims(connection, [
        {"id": "umich", "claim": "Eric is enrolled in Michigan MSCSE"},
    ])
    assert result["status"] == "migrated"
    assert not db.needs_migration(connection)
    assert "title" not in {row[1] for row in connection.execute("PRAGMA table_info(nodes)")}


def test_finalize_rebuilds_both_indexes(v0_store):
    """Every v0 vector was computed from a title and a summary that no longer
    exist, and the FTS columns changed shape."""
    connection = _connect(v0_store)
    migrate.run(connection, v0_store)
    migrate.set_claims(connection, [
        {"id": "apartment", "claim": "Eric's apartment is 2442 Leslie Circle"},
        {"id": "umich", "claim": "Eric is enrolled in Michigan MSCSE"},
    ])

    from claude_memory import retrieval

    hits = retrieval.search(connection, "Leslie Circle apartment", exclude_ids=None)
    assert any("Leslie" in hit["claim"] for hit in hits)


def test_detail_survives_the_rename(v0_store):
    connection = _connect(v0_store)
    migrate.run(connection, v0_store)
    migrate.set_claims(connection, [
        {"id": "apartment", "claim": "Eric's apartment is 2442 Leslie Circle"},
        {"id": "umich", "claim": "Eric is enrolled in Michigan MSCSE"},
    ])
    detail = connection.execute(
        "SELECT detail FROM nodes WHERE id = 'apartment'"
    ).fetchone()[0]
    assert "contracted to be short" in detail


def test_the_word_cap_is_enforced_during_migration(v0_store):
    """This is the one path by which every pre-existing node gets its claim. A
    migration that admitted 20-word claims would leave the store violating the
    invariant the whole redesign rests on."""
    connection = _connect(v0_store)
    migrate.run(connection, v0_store)

    with pytest.raises(InvariantError, match="max 8"):
        migrate.set_claims(connection, [
            {"id": "apartment",
             "claim": "Eric has an apartment in Ann Arbor at 2442 Leslie Circle"},
        ])


def test_run_is_idempotent(v0_store):
    connection = _connect(v0_store)
    first = migrate.run(connection, v0_store)
    second = migrate.run(connection, v0_store)
    assert second["pending"] == first["pending"]
    assert connection.execute("SELECT COUNT(*) FROM nodes").fetchone()[0] == 2


def test_rollback_restores_the_pre_migration_store(v0_store):
    connection = _connect(v0_store)
    migrate.run(connection, v0_store)
    connection.close()

    assert migrate.rollback(v0_store)["restored"]

    restored = sqlite3.connect(str(v0_store))
    columns = {row[1] for row in restored.execute("PRAGMA table_info(nodes)")}
    assert "summary" in columns and "claim" not in columns
    assert restored.execute("SELECT COUNT(*) FROM nodes").fetchone()[0] == len(V0_NODES)
    restored.close()


def test_both_entry_hooks_announce_an_unmigrated_store(v0_store):
    """A notice only at session start is not enough: a session already running
    when the store falls behind would go on treating memory as empty, which is
    indistinguishable from a user who has never told it anything.

    Session start and user prompt between them cover every path into a turn."""
    from claude_memory import hooks

    connection = _connect(v0_store)

    start = hooks.build_session_start_context({"cwd": "/tmp"}, connection)
    prompt = hooks.build_user_prompt_context({"prompt": "ok"}, connection)

    for block in (start, prompt):
        assert "MEMORY IS NOT WORKING" in block
        assert "Ask the user whether to migrate" in block
        assert str(v0_store) in block, "must name the store it actually found"


def test_the_notice_survives_the_triviality_gate(v0_store):
    """`should_retrieve` exists to keep irrelevant nodes out of 'ok' and 'yes'.
    A broken store is not a relevance question -- it is equally true either way."""
    from claude_memory import hooks

    connection = _connect(v0_store)
    assert hooks.build_user_prompt_context({"prompt": "ok"}, connection)


def test_stop_never_carries_the_notice(v0_store):
    """Stop's additionalContext forces the turn to continue, so anything
    unconditional here makes the conversation unable to end. UserPromptSubmit
    already fires on every message, so Stop has nothing to add.

    The maintenance reminder is suppressed too: telling an agent to capture what
    it learned is noise when every write would raise."""
    from claude_memory import hooks

    connection = _connect(v0_store)
    stops = [hooks.build_stop_context({"session_id": "s"}, connection)
             for _ in range(hooks.MAINTAIN_EVERY * 2)]
    assert not any(stops)


def test_a_migrated_store_says_nothing(v0_store):
    from claude_memory import hooks

    connection = _connect(v0_store)
    migrate.run(connection, v0_store)
    migrate.set_claims(connection, [
        {"id": "apartment", "claim": "Eric's apartment is 2442 Leslie Circle"},
        {"id": "umich", "claim": "Eric is enrolled in Michigan MSCSE"},
    ])
    assert hooks.migration_notice(connection) == ""
    assert "MEMORY IS NOT WORKING" not in hooks.build_session_start_context(
        {"cwd": "/tmp"}, connection
    )


def test_migrate_stops_the_daemon(v0_store, monkeypatch):
    """The daemon is the only long-lived process here: sessions reach the store
    through short-lived hook processes that pick up new code for free, while the
    daemon serves from whatever it imported at start-up."""
    from claude_memory import daemon

    calls = []
    monkeypatch.setattr(daemon, "stop", lambda: calls.append(True) or 4321)

    connection = _connect(v0_store)
    report = migrate.run(connection, v0_store)

    assert calls, "migration must not leave a stale reader running"
    assert report["daemon_stopped"] == 4321


def test_a_dead_daemon_does_not_block_migration(v0_store, monkeypatch):
    """Best-effort: the cost of a surviving daemon is a stale reader, not a
    corrupt store, so it must never be the thing that stops an upgrade."""
    from claude_memory import daemon

    def boom():
        raise OSError("no daemon here")

    monkeypatch.setattr(daemon, "stop", boom)

    connection = _connect(v0_store)
    assert migrate.run(connection, v0_store)["pending"] == 2
