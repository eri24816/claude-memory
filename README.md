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
| `raw` | Unread source text — a document section, chunked and indexed but not judged | — |
| `fact` | A proposition true **at least since** `window_start` | `[window_start, window_end]`; null end = unknown |
| `action` | Someone did something — user, agent, or third party | `window_start` (= end, or a range if durative) |
| `todo` | Something necessary or decided — a todo list or calendar item | `window_end` = due date, if any |
| `intention` | Someone wants to and may do something — not yet committed | when expressed |
| `idea` | A thought or proposal, large or small | when thought |
| `meta` | How this memory system works | — |
| `conv-pref` | How the agent should communicate or behave | — |
| `code-pref` | A coding constraint or convention | — |

### `raw`, and refinement on demand

Ingesting a wiki page cannot produce facts. A markdown section is prose that may
contain zero claims or twenty, and typing it as a `fact` about the world asserts
something the ingest never checked. So sections land as `raw`: chunked on headings,
embedded, searchable — and explicitly *not* believed. `raw` carries no `about_user`,
because nothing has read it closely enough to answer that.

Raw nodes are scored at `0.6 ×` the fused RRF score, so they surface but yield to any
refined node covering the same ground. When one does surface and an agent actually reads
it, the search result carries a hint: extract the claims and `supersede` the chunk with
them. Refinement is therefore paid for by attention already spent on a real question,
rather than by a batch LLM pass over 500 chunks that no one will ever ask about. The
corpus sharpens along the paths that get used.

Because supersession pointers would be destroyed by re-ingest — which deletes and rewrites
a file's derived nodes — they are carried across by section id, which is stable as long as
the heading survives. A rename loses the link but never the refined node.

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
| `conv-pref` | all |
| `fact` | `about_user`, not yet ended, starting within `lead_time` |
| `todo` | `about_user` and still open |
| `idea`, `action`, `intention` | `about_user`, 3 most recent of each |

```sql
   type IN ('meta', 'conv-pref')
OR (about_user AND type = 'fact'
    AND NOT stale
    AND (window_end IS NULL OR date('now') <= window_end)
    AND (window_start IS NULL
         OR window_start <= date('now', '+' || :lead_time)))
OR (about_user AND type = 'todo'
    AND superseded_by IS NULL AND NOT stale)
-- plus: 3 most recent each of idea / action / intention, about_user
-- autoload set = global-T1 + project-T1 (current project only)
```

`code-pref` is **not** autoloaded — meta instead tells the agent to fetch code-pref nodes
when it is about to write code, keeping coding conventions out of every non-coding
session's budget.

A fact enters T1 once its window starts within `lead_time` (30 days) and stays until the
window ends. The UMich enrolment autoloads from 2026-07-06 onward; a fact starting in
2028 does not.

Every set here is self-limiting except **open todos**, which shrink only when closed. If
T1 outgrows its budget, that is the category to cap first.

`scope` (`global | project:<name>`) is orthogonal to tier and fixes cross-project
siloing. **When in doubt, `global`** — a wrongly project-scoped fact is invisible
everywhere else and fails silently; a wrongly global one is merely noisy.

## Workflow

No user-facing CLI. The agent is the only caller; the user talks to the agent.

### Writing

| Trigger | Mechanism | Why |
|---|---|---|
| Every N turns | `Stop` hook injects a maintain-memory instruction | **The counter must live in the hook.** A model cannot reliably count turns, and a meta instruction alone loses to task load |
| Before compaction | `PreCompact` hook | The one point where uncaptured context is lost permanently |
| On request | user asks the agent to ingest a file or folder | Manual for now — **no auto-ingest** |

Meta describes *how* to maintain memory; the hooks decide *when*. Both are needed: meta
alone is model-discretion, hooks alone have no instructions to give.

### Reading

| Path | Trigger | Notes |
|---|---|---|
| **Autoload T1** | `SessionStart`, `PostCompact` | Compaction destroys the T1 block, so it must be re-injected |
| **Per-message retrieval** | `UserPromptSubmit` | Query = user message (+ light recent context). Small K, with a **relevance floor** so trivial messages inject nothing |
| **Active search** | agent decides | Documented in meta; the fallback for what auto-retrieval missed |

Per-message retrieval matters most: the recurring failure is not that the agent cannot
find a node, but that it does not know one exists to look for. Active search only fires
when the agent already suspects something is there.

**Both search paths exclude what T1 already loaded.** Retrieval competes for a budget the
autoload set has already spent, and a node returned verbatim into context it is already in
costs a slot twice over — worse, it reads as two independent sources agreeing when it is
one source counted twice. The exclusion happens in SQL, before `LIMIT`, or the dropped
rows would consume result slots and silently shrink the result set.

T1 **replaces the global `CLAUDE.md`**, which is retired (archived at
`archive/global-CLAUDE.md.retired`). Its rules were migrated to nodes first: coding
conventions to `code-pref`, tool and formatting habits to `conv-pref`, and its
memory-maintenance rules folded into the `meta` node, since they described routing into
the very file being retired. The behavioural change is real — `code-pref` is not
autoloaded, so those conventions now depend on the agent searching for them, which `meta`
instructs it to do before writing code.

### Agent-facing API

A programmatic layer is **mandatory, not stylistic**: inserting a node requires computing
an embedding, and search requires embedding the query — neither is expressible in SQL.

| Function | Purpose |
|---|---|
| `remember(nodes)` | insert / supersede, batched |
| `search(query, k, scope)` | hybrid RRF retrieval |
| `assemble_t1(scope)` | the categorical T1 set |
| `ingest(path)` | heading-split a file or folder |
| `sql(query)` | **read-only** escape hatch for the long tail |

Since the layer must exist anyway, it also enforces the invariants the agent would
otherwise violate silently:

- **Summaries are immutable** — no `UPDATE` on `summary`, only insert + supersede
- Supersession writes both sides and appends revisions stamped with `capture_run_id`
- Never supersede an `idea` or an umbrella `intention`
- `about_user` required for `fact`/`action`/`todo`/`intention`, null for `meta`/prefs
- `window_start <= window_end`; supersede target exists; no self or cyclic supersession
- `origin = 'original'` nodes are protected from `reindex`
- Writes keep `nodes_fts` and `nodes_vec` in sync with `nodes`

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
| `lead_time` | how early a future-dated fact enters T1 | 30 days |
| `recent_n` | per-type cap for idea / action / intention in T1 | 3 |
| `maintain_every` | turns between `Stop`-hook maintenance prompts | 5 |
| `retrieval_floor` | minimum RRF score for per-message injection | open |
| `N_redundant` | redundant hits before abstraction fires | open |
| `rrf_k` | RRF constant | 60 |
| `candidate_k` | per-index over-fetch before fusion | 200 |

`priority` is gone — T1 selection is categorical rather than ranked. `t1_budget` is only
a tripwire: if the categorical set exceeds it, cap open todos rather than reintroducing a
ranking function.

**RRF replaces weight tuning.** BM25 scores and cosine distances are on incomparable
scales; fusing by *rank* needs one constant you never touch.

$$\text{score}(d) = \sum_{i} \frac{1}{k + \text{rank}_i(d)}, \quad k \approx 60$$
