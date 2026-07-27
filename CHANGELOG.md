# Changelog

## 0.3.1 — 2026-07-27

- **The instructions are split by moment of use, each one owned in a single
  place.** `meta.md`, the memory skill and the Stop reminder had grown into
  three overlapping copies of the same guidance, and the copies had drifted.
  `meta.md` now holds what memory is, how to read it including the row format,
  and six enumerated capture triggers each naming its node type; the skill
  holds construction mechanics only; the reminder holds the forcing function
  alone and points at meta's triggers, dropping from ~1230 to 361 bytes per
  firing. The reminder can reference meta because `SessionStart` has no
  `matcher` and `session_start()` ignores `payload['source']`, so meta is
  re-injected on startup, resume, clear and compact alike. `meta.md`'s WRITING
  section was prose, which is the same holistic form the Stop hook was built to
  correct — it is now a checklist, with the "nothing to write is normal" clause
  out of final position. A test asserts meta still has six contiguous triggers,
  because nothing else catches a hand edit that leaves the pointer dangling.

- T1 ends with a **history block**: the last eight nodes written, oldest first,
  any type but `raw`. The categorical sets say what is true about the user and
  nothing says what was being worked on — a session's own output is spread
  across types and mostly capped away by `recent_n`. Superseded nodes stay in
  it with their `->` pointer, since the corrections are the best signal a
  sequence of claims carries.
- The inspector's Nodes tab pages. It fetched 200 rows and rendered them as if
  they were the store, so with 629 nodes a node past the newest 200 was absent
  with nothing on screen saying so — it read as "not in memory". `/api/nodes`
  now returns `{nodes, total, offset, limit}`, the tab shows `showing N of M`
  with a Load-more button, and the list scrolls in its own box so the count
  stays on screen. Ordering breaks ties on `id`: a bulk ingest gives hundreds
  of nodes the same `updated` second, and without a tiebreak a row could be
  skipped by one page and repeated by the next.
- `t1 --json` emits `{"nodes": ..., "history": ...}` rather than a bare list.

## 0.3.0 — 2026-07-26

- One row format everywhere — `claim|when|+|-> correction`. T1, search, the
  per-message block and dig all used to render a node their own way. Dates drop
  the century, and the year inside the current one: `07-17`, `27-07-31`.
- `dig` ends with the session's other nodes in the order they were written,
  three each side of the one dug, so an agent can walk sideways to the context a
  node was written in. Every reference it prints is a row rather than a claim.
- Retrieval no longer subtracts the autoloaded set. The nodes T1 preloads are
  the ones most likely to answer a question about the user, so excluding them
  meant a search for what the store knows best returned everything except it.
- `RAW_PER_QUERY` is 5, up from 2.
- The inspector's Nodes tab opens the dig instead of the detail field, and its
  table shares the same date rule and names what superseded a row.

## 0.2.1 — 2026-07-26

- The refinement mechanism is removed. `ingest` no longer carries supersession
  pointers across a re-ingest; an edited file has its chunks deleted and
  rewritten, and `refinements_kept` / `refinements_lost` are gone from its
  output. It guarded an event that happened zero times in four days of use.
- `capture-prompt.md` is deleted. It described a Stop-hook extraction stage that
  no code ever called, on the pre-0.1.0 `title`/`summary` schema. Every rule in
  it that was still true now lives in `meta.md` or the `memory` skill.
- The `memory` skill gains the window semantics per type — notably that a
  `todo`'s `window_end` is a due date, not when it stops being true — plus the
  granularity, restraint and supersession rules that had no other home.

## 0.2.0 — 2026-07-25

- `raw` and `ingest` return. A section's heading path is its claim and its body
  is its detail, so a chunk renders as one locator line carrying `+` and costs a
  dig to open.
- Raw hits are capped at two per mixed query (`RAW_PER_QUERY`), filtered in SQL
  before `LIMIT` so a capped-away chunk cannot consume a result slot. This
  replaces v0's 0.6 score demotion, which needed a refined counterpart to lose
  to and never had one. `--type raw` opts out.
- Nothing depends on refinement happening any more — it improves the store when
  it happens and costs nothing when it does not.
- `raw` is exempt from the eight-word claim cap and bounded at 80 characters
  instead: its claim is a heading path, not a compressed assertion. It requires
  a detail, and still declares no `about_user`, which keeps it out of autoload.

## 0.1.0 — 2026-07-25

- Every node is an eight-word `claim` plus optional `detail`. Only the claim
  renders, with a trailing `+` when detail exists; both are indexed.
- `id` is the handle, so `title` is gone. `dig` resolves an id, an exact claim,
  or an unambiguous fragment, and reports ambiguity as ambiguity.
- Preferences and meta become files: a rule has no history, only a current form,
  and files can be merged where nodes can only append. `code.md` is reached
  through the new `code-prefs` skill rather than preloaded, `meta.md` ships
  tracked at the repo root, and the program refuses to write it. The store moves
  to `settings/` beside them; `~/.claude` belongs to the harness.
- `raw` and `ingest` are removed until 0.2.0 reintroduces them with a per-query
  cap. Measured on the reference store, autoload drops 19,969 → 8,158 characters.
- `python -m claude_memory install` (also run by `init`) links both skills into
  `~/.claude/skills`, falling back to a Windows junction and then a copy, and
  seeds empty `conv.md` / `code.md` without overwriting. There is no migration
  path from 0.0.0 — everyone who clones the repo arrives fresh.

## 0.0.0 — 2026-07-21

The first working system, never tagged. Four days of real use produced the
measurements that motivated 0.1.0.

- Nodes carried a `title` and a one-sentence `summary`; the title was the handle
  and had to be unique.
- Conversational and coding preferences were nodes like any other, as was the
  autoloaded meta block.
- SQLite + FTS5 and `sqlite-vec` fused by Reciprocal Rank Fusion, behind
  `search` / `dig` and a T1 autoload block.
- SessionStart, UserPromptSubmit and Stop hooks; a daemon keeping the embedding
  model warm; the inspector on port 8765.
- `ingest` stored wiki markdown as `raw` nodes, to be refined on demand — 518
  were stored and none were ever refined.
