-- claude-memory schema. See README.md for the design rationale.

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS nodes (
    id              TEXT PRIMARY KEY,
    title           TEXT,
    summary         TEXT NOT NULL,          -- immutable
    type            TEXT NOT NULL,          -- raw|fact|action|todo|intention|
                                            -- idea|meta|conv-pref|code-pref
    about_user      INTEGER,                -- fact/action/todo/intention only
    scope           TEXT NOT NULL DEFAULT 'global',
    window_start    TEXT,
    window_end      TEXT,
    stale           INTEGER NOT NULL DEFAULT 0,
    superseded_by   TEXT REFERENCES nodes(id),
    origin          TEXT NOT NULL DEFAULT 'original',   -- original | derived
    parent          TEXT REFERENCES nodes(id),
    derived_from    TEXT,
    content_hash    TEXT,
    locator         TEXT,
    source_session  TEXT,
    updated         TEXT NOT NULL
);

-- The title is the agent-facing handle for digging into a node, so it has to
-- resolve to exactly one row. Enforced here rather than only in store.py,
-- because a duplicate makes a dig silently ambiguous.
CREATE UNIQUE INDEX IF NOT EXISTS nodes_title ON nodes(title);

CREATE INDEX IF NOT EXISTS nodes_window     ON nodes(window_start, window_end);
CREATE INDEX IF NOT EXISTS nodes_scope      ON nodes(scope);
CREATE INDEX IF NOT EXISTS nodes_type       ON nodes(type);
CREATE INDEX IF NOT EXISTS nodes_parent     ON nodes(parent);
CREATE INDEX IF NOT EXISTS nodes_superseded ON nodes(superseded_by);
CREATE INDEX IF NOT EXISTS nodes_derived    ON nodes(derived_from);

-- Append-only history. Undo a bad capture run by capture_run_id.
CREATE TABLE IF NOT EXISTS node_revisions (
    node_id         TEXT NOT NULL,
    revision        INTEGER NOT NULL,
    op              TEXT NOT NULL,          -- insert | supersede | stale | delete
    summary         TEXT,
    capture_run_id  TEXT,
    recorded_at     TEXT NOT NULL,
    PRIMARY KEY (node_id, revision)
);

CREATE INDEX IF NOT EXISTS node_revisions_run ON node_revisions(capture_run_id);

-- 1:1 relations live as columns on nodes because they sit on the hot retrieval
-- filters. Everything 1:many or many:many lives here.
CREATE TABLE IF NOT EXISTS node_edges (
    src_id  TEXT NOT NULL REFERENCES nodes(id),
    dst_id  TEXT NOT NULL REFERENCES nodes(id),
    rel     TEXT NOT NULL,                  -- motivates | relates
    PRIMARY KEY (src_id, dst_id, rel)
);

CREATE INDEX IF NOT EXISTS node_edges_dst ON node_edges(dst_id, rel);

-- Lexical half. rowid is aligned with nodes.rowid.
-- unicode61, not porter: stemming hurts exact identifier matching, and this
-- corpus is dense with code identifiers and proper nouns.
CREATE VIRTUAL TABLE IF NOT EXISTS nodes_fts USING fts5(
    title,
    summary,
    keywords,
    tokenize = 'unicode61 remove_diacritics 2'
);

-- Dense half. rowid is aligned with nodes.rowid. Requires the sqlite-vec
-- extension to be loaded first; see db.connect().
CREATE VIRTUAL TABLE IF NOT EXISTS nodes_vec USING vec0(
    embedding float[384]
);

-- Per-session Stop-hook turn counter. Lives in SQLite rather than a JSON file
-- so concurrent sessions get atomic increments for free instead of a hand-
-- rolled read-modify-write race; not a "node" -- it is agent-invisible state,
-- never searched or rendered.
CREATE TABLE IF NOT EXISTS hook_state (
    session_id  TEXT PRIMARY KEY,
    stop_count  INTEGER NOT NULL DEFAULT 0,
    updated     TEXT NOT NULL
);
