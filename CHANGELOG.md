# Changelog

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
