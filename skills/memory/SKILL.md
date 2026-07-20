---
name: memory
description: Command reference and node schema for Eric's claude-memory store. Load before writing a memory, superseding one, or retiring one — the autoloaded block says when to capture, this says how.
---

# Memory — how to write

The autoloaded `# Memory` block covers *when* to capture and how to search. This
is the execution detail: schema, and how to change what is already stored.

## Write

```bash
python -m claude_memory remember --file nodes.json
```

`nodes.json` is a JSON array. Use `--file`, not a pipe — PowerShell mangles stdin.

```json
[
  {
    "title": "Ann Arbor apartment",
    "summary": "Eric's Ann Arbor apartment is at 2442 Leslie Circle, lease starting 2026-07-17.",
    "type": "fact",
    "about_user": true,
    "window_start": "2026-07-17",
    "window_end": "2027-07-31"
  }
]
```

| Field | Notes |
|---|---|
| `title` | **Required, unique** — the handle everything else resolves by. Short noun phrase, specific enough to quote back: *"Ann Arbor apartment"*, not *"apartment"*. A `(2)` suffix in the result means you collided with an existing node; pick a better name. |
| `summary` | **Required.** One self-contained sentence that makes sense to a reader who never saw this session. Immutable once written. |
| `about_user` | **Required for `fact`/`action`/`todo`/`intention`.** Is this inside Eric's personal sphere? Not grammatical subject — *"my roommate moved in"* is `true`, *"Fizz rebranded"* is `false`. Gates the autoload set, so get it right. |
| `window_start` / `window_end` | ISO dates. **Convert relative to absolute** — "next August" → `2026-08-01`. Null end = unknown, not "forever"; leave it null only when the end is genuinely unknowable. |
| `scope` | `global` by default. `project:<name>` only if a session in another directory would not want it. **When in doubt, global** — a wrongly project-scoped node is invisible everywhere else, which fails silently. |

| Type | Meaning |
|---|---|
| `fact` | A proposition true **at least since** `window_start` |
| `action` | Someone did something — Eric, you, or a third party |
| `todo` | Something necessary or decided; a todo list or calendar item |
| `intention` | Someone wants to and may do something, not yet committed |
| `idea` | A thought or proposal |
| `conv-pref` | How you should communicate or behave |
| `code-pref` | A coding constraint or convention |
| `raw` | Unread document text. **Never write one** — only ingest creates these. |

Disambiguation: names an action someone may take → `intention`. Decided or
necessary → `todo`. Already happened → `action`. A concept or proposal → `idea`.

## Changing what is stored

Summaries are immutable. There is no update path.

**Reality changed** → supersede, reusing the old title so the name keeps pointing
at whatever is currently true:

```json
[{ "op": "supersede", "supersedes": "Ann Arbor apartment",
   "title": "Ann Arbor apartment", "type": "fact", "about_user": true,
   "summary": "Eric's lease at 2442 Leslie Circle was renewed through 2028-07-31.",
   "window_start": "2027-08-01", "window_end": "2028-07-31" }]
```

**No longer true, nothing replacing it** → `python -m claude_memory stale "<title>"`.
Use when Eric says he no longer intends something.

**You captured it wrong** → `remember` prints a `capture_run_id`;
`python -m claude_memory rollback <id>` undoes that whole batch.

Never supersede an `idea` — it stays valid however much work it spawns. Link
instead: `"edges": [{"rel": "motivates", "dst": "<title>"}]`.

## Refining `raw`

If a `raw` hit genuinely answered your question, keep that reading: write the
claim as a properly typed node and supersede the chunk with it. Only for chunks
you actually read and used — leaving the rest raw is correct, not a backlog.

## Search options beyond the basics

`--type <type>` restrict to one kind · `--limit N` (default 10) ·
`--scope project:<name>` include a project's nodes alongside global ·
`--json` raw rows with BM25 and vector ranks, for debugging retrieval itself.

## The background daemon

Per-message retrieval runs the embedding model on every substantive message.
A background process (`python -m claude_memory daemon start`) keeps that model
warm so only the first message of a cold machine pays the load; `SessionStart`
already triggers this automatically, so you should not normally need to touch
it. `daemon status` / `daemon stop` exist for when retrieval feels slow or you
want to confirm it is running — you do not need this for ordinary reads or
writes, which work with or without it.
