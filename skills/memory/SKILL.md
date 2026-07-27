---
name: memory
description: Field schema and command reference for the claude-memory store. Load before writing a memory, superseding one, or retiring one — the autoloaded block says when to capture, this says how.
---

# Memory — how to write

The autoloaded block covers *when* to capture and how to read. This is the execution detail: the schema, and how to change what is already stored. Assume the decision to capture has already been made.

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
| `claim` | **Required. Eight words maximum**, enforced. The whole node as far as anything that reads memory is concerned — it is what T1 and search render, and nothing truncates it. Compress by deleting function words, not by cutting the end. **No dates**: the window renders itself in the row, so a date in the claim wastes a word and drifts from the field. |
| `detail` | Optional, and **usually absent**. Everything that did not fit. Returned only by `dig`. Write it for a verification trail, a gotcha, or a correction — not to preserve a paragraph you did not want to compress. |
| `about_user` | **Required for `fact`/`action`/`todo`/`intention`.** Is this inside Eric's personal sphere? Not grammatical subject — *"my roommate moved in"* is `true`, *"Fizz rebranded"* is `false`. Gates the autoload set, so get it right. |
| `window_start` / `window_end` | ISO dates. **Convert relative to absolute** — "next August" → `2026-08-01`. Null end = unknown, not "forever". |
| `scope` | `global` by default. `project:<name>` only if a session in another directory would not want it. **When in doubt, global** — a wrongly project-scoped node is invisible everywhere else, which fails silently. |

| Type | Meaning | What the window means |
|---|---|---|
| `fact` | A proposition true **at least since** `window_start` | `[start, end]`; null end = unknown |
| `action` | Someone did something — Eric, you, or a third party | the day it happened: `start` = `end`, a range only if durative |
| `todo` | Something necessary or decided; a todo list or calendar item | `end` is the **due date**, if there is one |
| `intention` | Someone wants to and may do something, not yet committed | when it was expressed |
| `idea` | A thought or proposal. Never carries `about_user` | when it was thought |

Disambiguation: names an action someone may take → `intention`. Decided or necessary → `todo`. Already happened → `action`. A concept or proposal → `idea`.

### Writing a claim

Think headline, not sentence. The eight words carry the distinguishing terms; everything else goes, or moves to `detail`.

| Instead of | Write |
|---|---|
| "After arriving in Ann Arbor, Eric will apply for a Discovery credit card on 2026-08-06" | `Eric will apply for Discovery card` |
| "The schtasks command fails with access denied when creating an ONLOGON trigger" | `schtasks ONLOGON trigger requires elevation` |

### One node, one statement

**Split when the time fields differ.** A year-long lease and the single day someone moves in are two nodes, because one node carries one window — fuse them and neither window is true.

Restraint is part of the schema. This is a constraint on *construction*, not a reason to skip a trigger: never invent a node to look useful, and never pad one node into three. If what you are about to write is a near-duplicate of something stored, supersede that node instead of adding a second one — the store can append but never merge.

### Before you write

`dig` resolves an id, an exact claim, or an unambiguous claim fragment. If a fragment matches several nodes it says so and lists the ids — that is **not** "no match", and writing a new node in response would create a duplicate nothing can merge. Dig your near-neighbours before adding.

## Changing what is stored

Claims are immutable. There is no update path.

**Reality changed** → supersede. The id is the handle and never moves, so the new node simply states what is true now:

```json
[{ "op": "supersede", "supersedes": "eric-s-apartment-is-2442-leslie-circle",
   "claim": "Eric renewed the Leslie Circle lease", "type": "fact",
   "about_user": true, "window_start": "2027-08-01", "window_end": "2028-07-31" }]
```

**No longer true, nothing replacing it** → `python -m claude_memory stale "<handle>"`. For an `intention`, only when Eric says he no longer intends it — an intention nobody has acted on is still an intention, not a stale one.

**Only completion supersedes.** An `action` that finishes a `todo` supersedes it; climbing the earlier rungs does not, because an idea keeps its own validity after spawning an intention. Those are `motivates` edges, below.

**Supersede what you wrote earlier in this same session.** A working conversation reverses its own positions — something asserted at turn 12 and rejected by turn 20 leaves both in the store unless the later turn supersedes the earlier. The `supersedes` handle resolves nodes from this session exactly like older ones.

**You captured it wrong** → `remember` prints a `capture_run_id`; `python -m claude_memory rollback <id>` undoes that whole batch.

Never supersede an `idea` — it stays valid however much work it spawns. Link instead with an edge.

## Edges

Two `rel` values exist, and they mean different things.

- **`motivates`** — the commitment ladder only, `idea → intention → todo`. Use it exactly where you would otherwise supersede an idea, which is never allowed.
- **`relates`** — everything else. A request that triggered research links to what the research produced; two facts informing the same open question.

```json
[{ "claim": "Eric asked which US card to get", "type": "action",
   "about_user": true, "window_start": "2026-07-20", "window_end": "2026-07-20",
   "edges": [{ "rel": "relates", "dst": "eric-is-leaning-toward-zolve" }] }]
```

`dst` resolves like `supersedes`, including a node written earlier in the *same* batch. Both ends must exist; a `dst` naming nothing raises rather than writing a dangling edge.

## The preference files

Routing is in the autoloaded block: a preference or convention is an edit to `settings/conv.md` or `settings/code.md`, not a node. `remember` rejects those types and names the file.

Mechanics: **merge, never append.** Find the rule that already covers the ground and rewrite it to include the new case; add a new bullet only when nothing there is about the same thing. These are files precisely so a rule can be merged; the node store can only append. After merging, the file should be no longer than before unless genuinely new ground was covered.

## Raw chunks

`python -m claude_memory where` prints the resolved paths for the store and the settings files.

`raw` is the one type you do not write by hand — `ingest` produces it. A raw row is a document section: its claim is the heading path, so it always carries `+` and always needs a dig. If you dig one and it says something worth keeping, write that as an ordinary typed node — do not supersede the chunk. Re-ingesting the file deletes and rewrites its chunks, so anything hung off one is lost; the typed claim stands on its own and survives.
