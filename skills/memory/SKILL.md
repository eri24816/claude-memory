---
name: memory
description: Field schema and command reference for the claude-memory store. Load before writing a memory, superseding one, or retiring one — the autoloaded block says when to capture, this says how.
---

# Memory — how to write

The autoloaded block covers *when* to capture and how to read. This is the
execution detail: schema, and how to change what is already stored.

**Preferences do not live here.** A stated preference or correction is an edit to
`settings/conv.md`; a coding convention is an edit to `settings/code.md` (see the
`code-prefs` skill). `remember` rejects those types and names the file. They are
files precisely so a new rule can be *merged into* an existing one — the node
store can only append, which is how thirteen conv-prefs accumulated in four days
with several restating each other.

## Write

```bash
python -m claude_memory remember --file nodes.json
```

`nodes.json` is a JSON array. Use `--file`, not a pipe — PowerShell mangles stdin.

```json
[
  {
    "claim": "Eric's apartment is 2442 Leslie Circle",
    "type": "fact",
    "about_user": true,
    "window_start": "2026-07-17",
    "window_end": "2027-07-31"
  }
]
```

| Field | Notes |
|---|---|
| `claim` | **Required. Eight words maximum**, enforced. The whole node as far as anything that reads memory is concerned — it is what T1 and search render, and nothing truncates it. Compress by deleting function words, not by cutting the end. **No dates**: the window renders itself as `[2026-07-17..]`, so a date in the claim wastes a word and drifts from the field. |
| `detail` | Optional, and **usually absent**. Everything that did not fit. Returned only by `dig`. Write it for a verification trail, a gotcha, or a correction — not to preserve a paragraph you did not want to compress. |
| `about_user` | **Required for `fact`/`action`/`todo`/`intention`.** Is this inside Eric's personal sphere? Not grammatical subject — *"my roommate moved in"* is `true`, *"Fizz rebranded"* is `false`. Gates the autoload set, so get it right. |
| `window_start` / `window_end` | ISO dates. **Convert relative to absolute** — "next August" → `2026-08-01`. Null end = unknown, not "forever". |
| `scope` | `global` by default. `project:<name>` only if a session in another directory would not want it. **When in doubt, global** — a wrongly project-scoped node is invisible everywhere else, which fails silently. |

| Type | Meaning |
|---|---|
| `fact` | A proposition true **at least since** `window_start` |
| `action` | Someone did something — Eric, you, or a third party |
| `todo` | Something necessary or decided; a todo list or calendar item |
| `intention` | Someone wants to and may do something, not yet committed |
| `idea` | A thought or proposal. Never carries `about_user` |

Disambiguation: names an action someone may take → `intention`. Decided or
necessary → `todo`. Already happened → `action`. A concept or proposal → `idea`.

### Writing a claim

Think headline, not sentence. The eight words carry the distinguishing terms;
everything else goes, or moves to `detail`.

| Instead of | Write |
|---|---|
| "After arriving in Ann Arbor, Eric will apply for a Discovery credit card on 2026-08-06" | `Eric will apply for Discovery card` |
| "The schtasks command fails with access denied when creating an ONLOGON trigger" | `schtasks ONLOGON trigger requires elevation` |

A claim followed by `+` in rendered output means the node carries detail. If you
are about to act on such a row, dig it first — the eight words are a pointer, not
the node.

## Read

```bash
python -m claude_memory search "<query>" "<another angle>"
python -m claude_memory dig <id-or-claim> <another>
```

Both take several arguments in one call, and should be used that way: a tool call
is billed for its cached prefix once, so one call with three queries costs a
fraction of three calls.

`dig` resolves an id, an exact claim, or an unambiguous claim fragment. If a
fragment matches several nodes it says so and lists the ids — that is **not** "no
match", and writing a new node in response would create a duplicate nothing can
merge.

## Changing what is stored

Claims are immutable. There is no update path.

**Reality changed** → supersede. The id is the handle and never moves, so the new
node simply states what is true now:

```json
[{ "op": "supersede", "supersedes": "eric-s-apartment-is-2442-leslie-circle",
   "claim": "Eric renewed the Leslie Circle lease", "type": "fact",
   "about_user": true, "window_start": "2027-08-01", "window_end": "2028-07-31" }]
```

**No longer true, nothing replacing it** → `python -m claude_memory stale "<handle>"`.

**You captured it wrong** → `remember` prints a `capture_run_id`;
`python -m claude_memory rollback <id>` undoes that whole batch.

Never supersede an `idea` — it stays valid however much work it spawns. Link
instead with an edge.

## Edges

Two `rel` values exist, and they mean different things. `dig` renders both
directions — everything a node points at, and everything that points at it — so
an edge is how a later agent sees why a node exists and what it connects to.

- **`motivates`** — the commitment ladder only, `idea → intention → todo`. Use it
  exactly where you would otherwise supersede an idea, which is never allowed.
- **`relates`** — everything else. A request that triggered research links to what
  the research produced; two facts informing the same open question.

```json
[{ "claim": "Eric asked which US card to get", "type": "action",
   "about_user": true, "window_start": "2026-07-20", "window_end": "2026-07-20",
   "edges": [{ "rel": "relates", "dst": "eric-is-leaning-toward-zolve" }] }]
```

`dst` resolves like `supersedes`, including a node written earlier in the *same*
batch. Both ends must exist; a `dst` naming nothing raises rather than writing a
dangling edge.

## Other commands

`where` resolved paths for the store and settings files ·
`--type <type>` restrict a search · `--limit N` (default 10) ·
`--scope project:<name>` · `--json` raw rows with BM25 and vector ranks.

## The background daemon

Per-message retrieval runs the embedding model on every substantive message.
`python -m claude_memory daemon start` keeps it warm; `SessionStart` triggers
this automatically, so you should not normally need to touch it. `daemon status`
/ `daemon stop` exist for when retrieval feels slow.
