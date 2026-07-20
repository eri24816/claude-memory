# claude-memory

A tiered memory system for Claude: an autoloaded core plus hybrid BM25 + vector
retrieval over a SQLite node store, with automatic capture from conversations and the
wiki.

## Principle

**Tier is a computed function of metadata and today's date, not a folder.** Store a node
once; derive its tier at load time. Time passing re-tiers automatically.

## Tiers

| Tier | Mechanism | Bound |
|---|---|---|
| **T1** | Autoloaded every session | Hard token budget → top-K by priority |
| **T2** | Hybrid **BM25 + vector** retrieval | Unbounded |

No T3. BM25 covers exact/rare terms (names, IDs, error strings); vector covers semantic
recall. Redundancy is handled by abstraction nodes, not another tier.

## Node types

| Type | Meaning | Time |
|---|---|---|
| `fact` | A proposition true **at least since** `window_start` | `[window_start, window_end]`; null end = unknown |
| `action` | Someone did something — user, agent, or third party | `window_start` (= end, or a range if durative) |
| `todo` | Something necessary or decided — a todo list or calendar item | `window_end` = due date, if any |
| `intention` | Someone wants to and may do something — not yet committed | when expressed |
| `idea` | A thought or proposal, large or small | when thought |
| `meta` | How this memory system works | — |
| `conv-pref` | How the agent should communicate or behave | — |
| `code-pref` | A coding constraint or convention | — |

A `fact` asserts truth *from* `window_start`, **not** present truth. So a fact node is
never falsified by the passage of time — only superseded. "Fizz rebranded to Mine in
July 2026" stays true forever; whether it is still *current* is what `stale` and
`superseded_by` answer.

### The commitment ladder

```
idea  ──motivates──▶  intention  ──motivates──▶  todo  ──superseded_by──▶  action
```

An `idea` is a proposition; an `intention` is a prospective action someone *may* take; a
`todo` is one they have *decided on* or that is *necessary*; an `action` is one that
happened.

**Only the last rung is supersession.** Completing a `todo` closes it, so the `action`
supersedes it. The earlier rungs are **motivation**, not replacement — one idea can spawn
many intentions, actions, and projects, and it stays a valid idea throughout. Superseding
it would delete it from retrieval, since queries filter `superseded_by IS NULL`.

An `intention` goes `stale` when the user says they no longer intend it, or when
everything it motivated is done.

**Only `todo` reaches T1.** Deciding is what earns the context budget. Open todos are
`superseded_by IS NULL AND NOT stale`.

Disambiguation: if it names an action someone may take → `intention`. If it is a concept
or proposal → `idea`.

`fact`, `action`, `todo`, and `intention` carry **`about_user`** — *is this inside my
personal sphere?* Not grammatical subject: "my roommate moved in" is `true`; "Fizz
rebranded" and "LeCun invented LeNet" are `false`. This flag gates T1 —
`about_user = false` is **never** T1.

Examples: *"I was at NCKU 2021–2025"* → `fact`. *"I signed the lease"* → `action`.
*"Get an SSN"* → `todo`. *"I'm planning to get a credit card"* → `intention`. *"Herman
wants to explore gesture control"* → `intention`, `about_user: false`. *"Try slot
attention on symbolic music"* → `idea`.

## Truth and supersession

**Summaries are immutable.** A node is never edited to track reality; a new node
supersedes it. Three disjoint signals:

| Field | Means |
|---|---|
| `window_end` set | Stopped being true, **and we know when** |
| `stale` | Stopped being true, **end date unknown** |
| `superseded_by` | A newer node replaces this one — may *refine* rather than falsify |

```sql
-- currently true
NOT stale AND (window_end IS NULL OR date('now') <= window_end)
```

**Immutability does not cover errors.** Reality changing → `superseded_by` (both nodes
are valid history). A bad LLM extraction → `node_revisions` rollback by
`capture_run_id` (a false node must not persist as history). Two mechanisms, two
purposes.

## Tier function

T1 is **categorical, not ranked** — take these sets whole, capping only the volatile
types. No priority function is needed.

| Set | Rule |
|---|---|
| `meta` | all |
| `conv-pref`, `code-pref` | all |
| `fact` | `about_user` and not yet ended |
| `todo` | `about_user` and still open |
| `idea`, `action`, `intention` | `about_user`, 3 most recent of each |

```sql
   type IN ('meta', 'conv-pref', 'code-pref')
OR (about_user AND type = 'fact'
    AND NOT stale
    AND (window_end IS NULL OR date('now') <= window_end))
OR (about_user AND type = 'todo'
    AND superseded_by IS NULL AND NOT stale)
-- plus: 3 most recent each of idea / action / intention, about_user
-- autoload set = global-T1 + project-T1 (current project only)
```

The `fact` predicate checks only `window_end`, so a fact whose window *starts* in the
future is in T1 from the moment it is recorded — the UMich enrolment is autoloaded today,
weeks before 2026-08-05. This is deliberate, and it removes the need for a `lead_time`
parameter.

Every set here is self-limiting except **open todos**, which shrink only when closed. If
T1 outgrows its budget, that is the category to cap first.

`scope` (`global | project:<name>`) is orthogonal to tier and fixes cross-project
siloing. **When in doubt, `global`** — a wrongly project-scoped fact is invisible
everywhere else and fails silently; a wrongly global one is merely noisy.

## Storage and durability

**SQLite is primary.** Rule of thumb: *store what you search; point to what you might read.*

| Layer | Rebuildable? |
|---|---|
| `nodes`, `node_revisions` | **No — precious.** LLM extraction is expensive *and non-deterministic*; re-running yields a different node set, not a restoration |
| `nodes_fts`, `nodes_vec` | **Yes — disposable.** Reparse + re-embed from `nodes` |

- **Never index raw transcripts** — huge and mostly noise. They stay grep-only, reached
  via `source_session`.
- **Path is a locator, never identity** — the wiki lives in iCloudDrive, which produces
  conflict copies and renames.
- Backup by daily `VACUUM INTO` snapshot, not git (a `.db` in git is an undiffable blob).
- WAL mode, so the capture hook writes while sessions read.

## Ingestion — one unit, two mechanisms

Everything indexed is a **node**; a wiki section *is* a node, so the id space stays
unified (RRF requires that).

| Source | Mechanism | Cost | Path back | `origin` |
|---|---|---|---|---|
| Wiki page | deterministic heading split | ~free | re-split anytime | `derived` |
| Wiki assertion | selective LLM extraction | moderate | re-extract | `derived` |
| Conversation | LLM identify + summarize | expensive | **none** | `original` |

Heading-split beats LLM extraction for the bulk: free, lossless, and the headings are
already the user's own curation. `reindex` may regenerate `derived` nodes; it must
**never** touch `original` ones.

Claude Code sessions are sweepable from `~/.claude/projects/**/*.jsonl`. claude.ai web
chats are not enumerable by any tool — they require an account data export, so that half
of capture is inherently batch and manually triggered.

## Indexing

Both indexes target the same units over the same id space — splitting corpora between
BM25 and vector is not hybrid search.

| | BM25 document | Embedding |
|---|---|---|
| Composition | title + summary + type + tags + aliases | summary (title-prefixed) |
| Why | **superset** — extra tokens only help lexical matching | **focused** — metadata dilutes the vector |

**BM25** = ranked lexical retrieval over the index. **grep** = unranked exact search over
raw files, a deliberate dig-down. Different mechanisms.

## Abstraction

When a query returns more than `N_redundant` semantically redundant hits, a subagent
clusters them into an abstraction node; children get `parent` set and collapse under it
at retrieval time, returning summary + child count. Children are never deleted.
Precision by default, recall on demand.

## Pipeline

1. **Capture** — hook extracts candidate nodes from live and abandoned sessions
2. **Route** — classify `type / scope / about_user / window`
3. **Store** — write node + revision, stamped with `capture_run_id`
4. **Consolidate** — daily re-tier (free) + redundancy-triggered abstraction (expensive)
5. **Assemble** — SessionStart builds T1 under budget

Steps 1–2 are the write side; tiering without them is just hand-saving.

**Supersede candidates must include nodes written earlier in the same session.** A
design conversation reverses positions as it goes; if the extractor only sees
pre-existing nodes, turn 20 cannot supersede turn 12 and the store accumulates
contradictions. This is what carries the load now that there is no curated/fleeting
distinction. See [capture-prompt.md](capture-prompt.md).

## Implementation

**SQLite + FTS5 + sqlite-vec.** FTS5 ships real `bm25()`. Pin sqlite-vec **v0.1.9**,
flat index only — it is pre-v1 and single-maintainer, but touches only the disposable
layer. Embeddings: FastEmbed `bge-small-en-v1.5` (384-dim), CPU, no torch dependency. No
ANN — 10k nodes ≈ 15 MB of floats, brute force is single-digit ms. Fallback: LanceDB.

```sql
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE nodes (
    id              TEXT PRIMARY KEY,
    title           TEXT,
    summary         TEXT NOT NULL,          -- immutable
    type            TEXT NOT NULL,          -- fact|action|todo|intention|idea|
                                            -- meta|conv-pref|code-pref
    about_user      INTEGER,                -- fact/action/todo/intention only
    scope           TEXT NOT NULL DEFAULT 'global',
    window_start    TEXT,
    window_end      TEXT,
    stale           INTEGER NOT NULL DEFAULT 0,
    superseded_by   TEXT REFERENCES nodes(id),
    origin          TEXT NOT NULL,          -- original | derived
    parent          TEXT REFERENCES nodes(id),
    derived_from    TEXT,
    content_hash    TEXT,
    locator         TEXT,
    source_session  TEXT,
    priority        REAL NOT NULL DEFAULT 0,
    updated         TEXT NOT NULL
);

CREATE INDEX nodes_window     ON nodes(window_start, window_end);
CREATE INDEX nodes_scope      ON nodes(scope);
CREATE INDEX nodes_type       ON nodes(type);
CREATE INDEX nodes_parent     ON nodes(parent);
CREATE INDEX nodes_superseded ON nodes(superseded_by);

CREATE TABLE node_revisions (
    node_id         TEXT NOT NULL,
    revision        INTEGER NOT NULL,
    op              TEXT NOT NULL,          -- insert | supersede | delete
    summary         TEXT,
    capture_run_id  TEXT,
    recorded_at     TEXT NOT NULL,
    PRIMARY KEY (node_id, revision)
);

CREATE INDEX node_revisions_run ON node_revisions(capture_run_id);

-- 1:1 relations stay as columns above (superseded_by, parent, derived_from) because
-- they sit on the hot retrieval filters. Everything 1:many or many:many lives here.
CREATE TABLE node_edges (
    src_id  TEXT NOT NULL REFERENCES nodes(id),
    dst_id  TEXT NOT NULL REFERENCES nodes(id),
    rel     TEXT NOT NULL,                  -- motivates | relates
    PRIMARY KEY (src_id, dst_id, rel)
);

CREATE INDEX node_edges_dst ON node_edges(dst_id, rel);

-- unicode61, not porter: stemming hurts exact identifier matching, and this
-- corpus is dense with code identifiers and proper nouns.
CREATE VIRTUAL TABLE nodes_fts USING fts5(
    title, summary, keywords,
    tokenize = 'unicode61 remove_diacritics 2'
);

CREATE VIRTUAL TABLE nodes_vec USING vec0(
    node_rowid INTEGER PRIMARY KEY,
    embedding  FLOAT[384]
);
```

### Hybrid retrieval (RRF)

`bm25()` returns negative scores — more negative is better, so ascending is best-first.
Over-fetch then filter: sqlite-vec KNN returns a global top-k.

```sql
WITH lexical AS (
    SELECT rowid AS node_rowid,
           ROW_NUMBER() OVER (ORDER BY bm25(nodes_fts)) AS rank
    FROM nodes_fts
    WHERE nodes_fts MATCH :query
    LIMIT :candidate_k
),
semantic AS (
    SELECT node_rowid,
           ROW_NUMBER() OVER (ORDER BY distance) AS rank
    FROM nodes_vec
    WHERE embedding MATCH :query_embedding
      AND k = :candidate_k
)
SELECT n.id, n.title, n.summary, n.type,
       COALESCE(1.0 / (:rrf_k + l.rank), 0)
     + COALESCE(1.0 / (:rrf_k + s.rank), 0) AS score
FROM nodes n
LEFT JOIN lexical  l ON l.node_rowid = n.rowid
LEFT JOIN semantic s ON s.node_rowid = n.rowid
WHERE (l.rank IS NOT NULL OR s.rank IS NOT NULL)
  AND n.scope IN ('global', :project_scope)
  AND n.parent IS NULL                      -- collapse to abstraction parents
  AND n.superseded_by IS NULL               -- current versions only
ORDER BY score DESC
LIMIT :final_k;
```

Dig-down replaces `parent IS NULL` with `parent = :abstraction_id`. History queries drop
the `superseded_by` clause.

## Parameters

| Parameter | Meaning | Default |
|---|---|---|
| `t1_budget` | autoload cap — a ceiling to alarm on, not a ranking input | 8,000 chars (~2k tokens) |
| `recent_n` | per-type cap for idea / action / intention in T1 | 3 |
| `N_redundant` | redundant hits before abstraction fires | open |
| `rrf_k` | RRF constant | 60 |
| `candidate_k` | per-index over-fetch before fusion | 200 |

`priority` and `lead_time` are gone: T1 selection is categorical rather than ranked, and
the `fact` predicate already admits future-dated windows. `t1_budget` is now only a
tripwire — if the categorical set exceeds it, cap open todos rather than introducing a
ranking function.

**RRF replaces weight tuning.** BM25 scores and cosine distances are on incomparable
scales; fusing by *rank* needs one constant you never touch.

$$\text{score}(d) = \sum_{i} \frac{1}{k + \text{rank}_i(d)}, \quad k \approx 60$$
