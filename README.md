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

## Claim and detail

Every node is an **8-word claim** plus optional **detail**. Only the claim is ever
rendered; `dig` returns the detail.

v0 stored a `summary` contracted to be one sentence. It averaged **684 characters** —
not through carelessness, but because detail had nowhere else to live. Two things
followed. Autoload grew without bound, since a fact leaves T1 only by supersession or an
expired window. And supersession lost its granularity: correcting one clause of a
six-claim paragraph meant rewriting the paragraph, so nobody did.

The cap is in **words, not characters**. A character budget produces truncation-shaped
prose; a word budget forces the function words out, which is what turns *"After arriving
in Ann Arbor, Eric will apply for a Discovery credit card"* into *"Eric will apply for
Discovery card"*. Dates never belong in a claim — the window renders itself.

Compression is a **display** decision. Both FTS and the vector index cover claim *and*
detail, so nothing becomes unfindable by being unrendered. A rendered claim carries a
trailing `+` when detail exists, so digging is an informed choice rather than a gamble
on whether the eight words were the whole node.

## Node types

| Type | Meaning | Time |
|---|---|---|
| `fact` | A proposition true **at least since** `window_start` | `[window_start, window_end]`; null end = unknown |
| `action` | Someone did something — user, agent, or third party | `window_start` (= end, or a range if durative) |
| `todo` | Something necessary or decided — a todo list or calendar item | `window_end` = due date, if any |
| `intention` | Someone wants to and may do something — not yet committed | when expressed |
| `idea` | A thought or proposal, large or small | when thought |
| `raw` | An ingested document section — a chunk, not a claim | none |

Rules are **not** node types. `meta`, `conv-pref` and `code-pref` are files in
`settings/` — see below.

`raw` is the one type nothing asserts. Its claim is the heading path its author wrote and
its body is the detail, so it is always followed by `+` and always costs a dig to open —
honest about being a pointer rather than a statement. It declares no `about_user`, which
is what keeps it out of the autoload set: nothing has read the chunk closely enough to
answer that question, and guessing would be a claim the ingest never made.

## Rules are files, not nodes

A fact has a history; a rule has only a current form. Storing rules as nodes bought
immutability nobody wanted and cost the one operation that mattered: **consolidation**.
Thirteen conv-prefs accumulated in four days of use, several restating each other,
because a node can only be appended or superseded — never merged. A file can be
rewritten in place.

| File | Preloaded | Holds |
|---|---|---|
| `settings/meta.md` | yes | How this memory system works |
| `settings/conv.md` | yes | How the agent should communicate and behave |
| `settings/code.md` | no — `code-prefs` skill | Coding constraints and conventions |

They live in `settings/` beside the store, deliberately **not** under `~/.claude/`, which
belongs to the Claude Code harness. `CLAUDE_MEMORY_SETTINGS` relocates the whole
directory. HTML comments are stripped before a file reaches context, so these files can
carry editorial notes for free.

### `raw`, and why refinement on demand failed

Ingested wiki sections landed as `raw`: chunked on headings, embedded, searchable — and
explicitly not believed. That part was sound. The mistake was the plan for improving
them.

Raw nodes were scored at `0.6 ×` the fused RRF score so they would yield to any refined
node covering the same ground, and a search hint invited the agent to extract the claims
and supersede the chunk. Refinement would then be paid for by attention already spent on
a real question, rather than by a batch pass over 500 chunks nobody would ask about.

**Measured after four days of daily use: 518 raw nodes, 0 refined.** Not slow uptake —
zero. A hint competes with the task, and the task always wins; the same lesson the `Stop`
hook was invented to encode. The demotion was collateral damage: with nothing ever
refined, there was no counterpart for a raw hit to lose to, so 0.6 only made raw weaker
at the one job it did well.

0.1.0 removed `raw` entirely. 0.2.0 brings it back under a **per-query cap**: at most
`RAW_PER_QUERY` (5) raw hits in any mixed search, filtered in SQL before `LIMIT` so a
capped-away chunk cannot consume a result slot.

The cap succeeds where the demotion failed because it asks an answerable question. "Is
this chunk worth less than a claim?" needs a refined counterpart to compare against, and
there was never one; "how much of a single query may be chunks?" is answerable with
nothing in the store but chunks. It is also indifferent to corpus size — 500 sections or
50,000, the sixth one is out — and it does not weaken the hits it keeps: the five that
survive arrive at full score, in the rank they earned.

Two limits now bound raw, and they are not the same limit. `MAX_PER_SOURCE` holds any one
*document* to two hits, because sections of a page are near-identical in embedding space.
`RAW_PER_QUERY` holds the whole *corpus* to five, which is what a wiki of a thousand pages
needs and what per-source cannot do. An explicit `--type raw` opts out of the per-query
cap: it exists to stop a mixed result set being swamped, and a search that asked for
chunks and nothing else is not being swamped.

Refinement is gone entirely, not merely optional. `ingest` no longer preserves anything
across a re-ingest: an edited file has its chunks deleted and rewritten, because a chunk
is regenerable from the document it came from and the machinery that protected
supersession pointers was guarding an event that occurred zero times in four days of use.
A chunk worth reading is worth writing a typed claim about, and that claim is an ordinary
node that stands on its own.

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

T1 has two halves with opposite economics, so they are assembled by opposite rules.

**The pref files** (`settings/meta.md`, `settings/conv.md`) go in **whole**. They are
behavioural: a rule the agent must fetch is a rule it will not follow. They are also
bounded, because a file can be rewritten — the reason they stopped being nodes.

**Nodes** contribute **one claim each**, because they grow without limit and are looked
up on demand. Categorical, not ranked — take these sets whole, capping only the volatile
types.

| Set | Rule |
|---|---|
| `fact` | `about_user`, not yet ended, starting within `lead_time` |
| `todo` | `about_user` and still open |
| `idea`, `action`, `intention` | `about_user`, 3 most recent of each |
| history | the last `history_n` nodes written, any type but `raw`, oldest first |

```sql
   (about_user AND type = 'fact'
    AND NOT stale
    AND (window_end IS NULL OR date('now') <= window_end)
    AND (window_start IS NULL
         OR window_start <= date('now', '+' || :lead_time)))
OR (about_user AND type = 'todo'
    AND superseded_by IS NULL AND NOT stale)
-- plus: 3 most recent each of idea / action / intention, about_user
-- autoload set = global-T1 + project-T1 (current project only)
```

`settings/code.md` is **not** preloaded — it is the largest of the three files and
irrelevant to every session that writes no code. It is reached through the `code-prefs`
skill, and `meta.md`, which *is* preloaded, carries the trigger line. That last part is
not decoration: this codebase already ran the experiment where a hint was supposed to
prompt on-demand work, and got zero uptake in 518 opportunities. The instruction has to
be in context before the moment it applies.

Measured on the reference store, this took autoload from 19,969 characters to 8,158 —
and, more importantly, changed the growing term from ~684 characters per fact to ~67.

**History is the one part of T1 that is a sequence rather than a set.** Everything above
it is what currently holds; the history block is the last eight nodes written, in the
order they were written, and it answers the question the categorical sets structurally
cannot: not "what is true about the user" but "what were we doing". A session's own
output is spread across types and mostly capped away by `recent_n`, so without it every
session opens on the same standing facts and re-derives the thread of work from whatever
the user happens to say first. Superseded nodes stay in it, carrying the `->` pointer:
a history that hides its corrections hides its best signal. `raw` is excluded — one
`ingest` writes hundreds of chunks in a pass, and a single wiki would be the whole
history.

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

Both hooks call into a background daemon (`claude_memory.daemon`) that keeps fastembed's
ONNX model warm across a whole session instead of cold-starting it — about 1.2s — in a
fresh process on every message. `SessionStart` fires `ensure_running()` fire-and-forget, so
warmup happens while Eric is still reading the autoloaded block; `UserPromptSubmit` tries
the daemon first and falls back to computing in-process if nothing answers yet, so a
message is never dropped just because the daemon has not come up. The daemon persists
deliberately, with no idle timeout — `python -m claude_memory daemon status` / `stop`.

**The id is the handle; the claim is what gets shown.** v0 used a unique `title` for both,
which worked only because the title was never rendered into context. Once the handle *is*
the displayed text, uniqueness becomes a liability: collisions can only be resolved by
suffixing, and `Ann Arbor apartment (2)` is meaningless as English in a block the agent
reads as fact. Claims are therefore not unique, and `resolve` accepts an id, an exact
claim, or an unambiguous fragment.

**Ambiguity is not absence.** A fragment matching several nodes raises `AmbiguousHandle`
and lists the ids, rather than returning "not found". Collapsing the two is worse than it
sounds: told that nothing matched, the agent's correct next move is to write the memory —
producing a duplicate an append-only store can never merge.

Supersession no longer moves anything. The id never changes, so a supersession is an
insert plus a pointer.

Every listing uses one row format — `claim|when|+|-> correction` — because T1, search,
the per-message block and dig's session block are all read by the same reader, and four
dialects meant learning the punctuation four times. Each field after the claim answers a
question that would otherwise cost a dig: is there more (`+`), when was it true (`when`),
and has something replaced it (`->`, naming the claim that currently stands, not the
immediate successor). Trailing empty fields are dropped; interior ones are kept, so a
field's position never depends on whether an earlier one was filled. Dates drop the
century and, within the current year, the year: `07-17`, against `27-07-31` for one
outside it, which reads as visibly not-this-year rather than silently wrong every
January.

Retrieved hits carry a single `# memory that could be useful:` heading and
**nothing truncated**: v0
clipped content at 400 characters and had to instruct the agent to dig "whenever a
truncated row looks like it matters", which made dig repair work for a renderer that
could not tell the whole truth. A claim is complete by construction, so a row is either
the whole node or a claim plus `+`.

**Retrieval returns autoloaded nodes like any other.** Until 0.3.0 both search paths
subtracted the T1 set, on the reasoning that a node already verbatim in context costs a
slot twice and reads as two sources agreeing when it is one counted twice. The cost of
that turned out to be worse than the duplication: the nodes most likely to answer a
question about the user are exactly the ones T1 preloads, so a search for something the
store knows best returned everything except the best answer, and a `dig` on a claim the
agent could see in its own context reported a hit while `search` for the same words did
not. A duplicate row is cheap and obvious. A silently withheld one is neither.

T1 **replaces the global `CLAUDE.md`**, which is retired (archived at
`archive/global-CLAUDE.md.retired`). Its rules became nodes in v0 and files again in
0.1.0 — coding conventions in `settings/code.md`, tool and formatting habits in
`settings/conv.md`, memory-maintenance rules in `settings/meta.md`.

That round trip is worth naming honestly. `CLAUDE.md` was a file, and files were replaced
by nodes to get retrieval, scoping and time windows. Rules turned out to want none of
those: they have no window, they are needed unconditionally or not at all, and what they
did want — merging — is exactly what a node cannot do. Facts kept the node store; rules
went home. The thing that changed is not the storage medium but which properties each
kind of memory actually needs.

### Installing

`python -m claude_memory init` creates the store, creates the two empty rule
files in `settings/`, and links both skills into `~/.claude/skills`. It is
idempotent, and `install` runs the last two on their own.

`meta.md` is not seeded, because it is tracked in the repo — it describes the
system rather than the user, so it arrives with the clone and updates with a
`git pull`. It is also what tells the agent this system exists at all: without
it the agent falls back to the file-based memory in its system prompt and writes
markdown nothing here reads, a failure that is completely silent.

The skills are **linked, not copied**: a copy goes on teaching whatever schema it
was copied from long after the repo has moved on.
Symlink where the platform allows one, a directory junction on Windows (no
elevation needed), and only then a copy — which reports `"linked": false`,
because that is the one case where a repo edit does not reach the installed
skill. See [INSTALL.md](INSTALL.md).

### Agent-facing API

A programmatic layer is **mandatory, not stylistic**: inserting a node requires computing
an embedding, and search requires embedding the query — neither is expressible in SQL.

| Function | Purpose |
|---|---|
| `remember(nodes)` | insert / supersede, batched |
| `search(query, k, scope)` | hybrid RRF retrieval |
| `assemble_t1(scope)` | the categorical T1 set |
| `session_history(scope, n)` | the last n nodes written, oldest first |
| `dig(handles...)` | expand nodes by id or claim, batched |
| `sql(query)` | **read-only** escape hatch for the long tail |

`search` and `dig` both take several arguments per call. A tool call is billed for its
cached prefix once, so three queries in one call cost a fraction of three calls — and
multi-angle search is the normal way a node actually gets found, not an optimisation.

Since the layer must exist anyway, it also enforces the invariants the agent would
otherwise violate silently:

- **Claims are immutable** — no `UPDATE` on `claim` or `detail`, only insert + supersede
- **Claims are at most 8 words** — the cap the autoload budget rests on
- Retired types (`conv-pref`, `code-pref`, `meta`) are rejected *by name*, pointing at the
  file they moved to; a bare "unknown type" would send the agent hunting for a typo
- Supersession writes both sides and appends revisions stamped with `capture_run_id`
- Never supersede an `idea` or an umbrella `intention`
- `about_user` required for `fact`/`action`/`todo`/`intention`, null for `idea`
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
  via `source_session` (which session) and `locator` (which event's `uuid` within it) —
  the same two-column pattern wiki nodes already use as `derived_from`/`locator`, just
  pointing at a transcript instead of a file. Both default automatically for an
  `origin='original'` node written live: `CLAUDE_CODE_SESSION_ID` gives `source_session`
  for free, and `locator` defaults to the most recent message Eric actually typed —
  skipping tool-result turns, which the Anthropic API also encodes as `role=user` and
  which are almost always the literal most recent one, not what he asked. Both defaults
  apply only when `source_session` actually resolves to the live session, so a retroactive
  write describing a *different*, past session's content is never stamped with a locator
  pointing at the wrong transcript.
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
distinction. The rule lives in the `memory` skill, alongside the rest of the write
schema.

## Implementation

**SQLite + FTS5 + sqlite-vec.** FTS5 ships real `bm25()`. Pin sqlite-vec **v0.1.9**,
flat index only — it is pre-v1 and single-maintainer, but touches only the disposable
layer. Embeddings: FastEmbed `bge-small-en-v1.5` (384-dim), CPU, no torch dependency. No
ANN — 10k nodes ≈ 15 MB of floats, brute force is single-digit ms. Fallback: LanceDB.

**The schema lives in [`claude_memory/schema.sql`](claude_memory/schema.sql)**, not here.
This section used to inline a copy, and by 0.1.0 it had drifted: it still declared
`title`, `summary`, a `priority` column that was never implemented, and the pref types
that are now files. A second copy of a schema is a second thing to forget to update.

The shape worth stating in prose, because it is a design decision rather than a detail:
1:1 relations (`superseded_by`, `parent`, `derived_from`) are columns on `nodes` because
they sit on the hot retrieval filters; everything 1:many or many:many lives in
`node_edges`. `nodes_fts` and `nodes_vec` align by `rowid` with `nodes`, index claim
*and* detail, and are both disposable — rebuilt wholesale by any reindex.

### Hybrid retrieval (RRF)

`bm25()` returns negative scores — more negative is better, so ascending is best-first.
Over-fetch then filter: sqlite-vec KNN returns a global top-k.

Dig-down replaces `parent IS NULL` with `parent = :abstraction_id`. History queries drop
the `superseded_by` clause.

## Parameters

| Parameter | Meaning | Default |
|---|---|---|
| `t1_budget` | autoload cap — a ceiling to alarm on, not a ranking input | 8,000 chars (~2k tokens) |
| `lead_time` | how early a future-dated fact enters T1 | 30 days |
| `recent_n` | per-type cap for idea / action / intention in T1 | 3 |
| `history_n` | nodes in T1's history block | 8 |
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
