-- claude-memory schema v1 (0.1.0). See README.md for the design rationale.
--
-- v1 replaces (title, summary) with (claim, detail). The old summary was
-- contracted to be one sentence and averaged 684 characters in practice --
-- not from carelessness, but because detail had nowhere else to live. Giving
-- it a home makes the short form followable: `claim` is capped at 8 words and
-- is the only thing rendered by default, `detail` holds the rest and is
-- returned only by dig.
--
-- The version lives in PRAGMA user_version and is set by migrate/init, never
-- by applying this file -- an un-migrated store must stay recognisable as one
-- even though every CREATE below is IF NOT EXISTS and would no-op against it.

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS nodes (
    id              TEXT PRIMARY KEY,
    claim           TEXT NOT NULL,          -- <= 8 words, immutable, rendered
    detail          TEXT,                   -- immutable; NULL is the common case
    type            TEXT NOT NULL,          -- fact|action|todo|intention|idea
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

-- No unique index on claim, deliberately. v0 made `title` unique because it was
-- the dig handle; now that the handle is `id` and the claim is rendered text, a
-- uniqueness constraint would only produce "(2)" suffixes -- visible in T1, and
-- meaningless as English. Two nodes may legitimately assert the same claim at
-- different times.
CREATE INDEX IF NOT EXISTS nodes_claim      ON nodes(claim);

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
    claim           TEXT,
    detail          TEXT,
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
-- Indexes claim AND detail even though only claim is rendered: compression is a
-- display decision, and indexing the 8 words alone would quietly destroy recall
-- for everything the detail says.
-- unicode61, not porter: stemming hurts exact identifier matching, and this
-- corpus is dense with code identifiers and proper nouns.
CREATE VIRTUAL TABLE IF NOT EXISTS nodes_fts USING fts5(
    claim,
    detail,
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
