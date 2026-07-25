# Carrying a pre-0.1.0 store into 0.1.0

**If you are the user:** type `migrate memory` in Claude Code. The agent reads
this file and does the rest.

**If you are the agent:** you are executing this. Read the whole file first.

---

## What this is

The old store is **never modified**. It is opened read-only, and you re-write its
nodes into the new store with the ordinary `remember` command. There is no schema
conversion, no backup step and no rollback, because nothing is ever at risk: the
old file stays exactly where it is, and stays valid, whatever happens here.

Stopping halfway is fine. The new store's contents *are* the progress record —
resume by looking at what is already in it.

| v0 | 0.1.0 |
|---|---|
| `title` + `summary` (one sentence, ~700 chars in practice) | `claim` (≤ 8 words) + optional `detail` |
| `conv-pref` / `code-pref` / `meta` nodes | files in `settings/` |
| `raw` wiki chunks | dropped; 0.2.0 rebuilds them from the wiki |

## 1. See what is waiting

```bash
python -m claude_memory migration status
```

Reports the old store's path, how many nodes are to be carried, and the breakdown
by type. `raw` is excluded — those are wiki chunks and are not carried.

The old daemon was already stopped when the new store was created. You do not
need to stop anything else: sessions reach memory through short-lived processes.

## 2. Read a batch

```bash
python -m claude_memory migration list --limit 25 --offset 0
```

Returns old nodes as JSON — `id`, `title`, `summary`, `type`, `about_user`,
`scope`, `window_start`, `window_end`, `locator`, `source_session`. Superseded
nodes are already filtered out.

## 3. Write them into the new store

For each node, compress `title` + `summary` into a **claim of at most 8 words**,
and keep `detail` only when something is genuinely lost without it.

```bash
python -m claude_memory remember --file batch.json
```

```json
[
  {"claim": "Eric's apartment is 2442 Leslie Circle",
   "type": "fact", "about_user": true,
   "window_start": "2026-07-17", "window_end": "2027-07-31"},

  {"claim": "schtasks ONLOGON trigger requires elevation",
   "type": "fact", "about_user": false, "window_start": "2026-07-24",
   "detail": "Fails with 'Access is denied' even with /RU set to the current user and /RL LIMITED. The working unelevated path is a .lnk in the Startup folder, created via the WScript.Shell CreateShortcut COM object."}
]
```

Carry `window_start`, `window_end`, `about_user` and `scope` across unchanged.
Do **not** carry `id` — the new store derives ids from claims.

### Writing a good claim

- **Eight words maximum**, enforced by `remember`. Compress by deleting function
  words, not by truncating: *"Eric will apply for Discovery card"*, not *"After
  arriving in Ann Arbor, Eric will apply for the…"*.
- **No dates.** The window renders itself as `[2026-08-06..]`. A date in the
  claim wastes a word and drifts from the field.
- **Front-load the distinguishing term** — the claim is what search matches on.
- **`detail` is usually absent.** Keep it for a verification trail, a gotcha, or
  a correction. Preserving every old summary wholesale just moves the bloat.

### The three types that are no longer nodes

`conv-pref`, `code-pref` and `meta` must **not** go through `remember` — it will
reject them and name the file they belong in.

- `conv-pref` → append to `settings/conv.md`
- `code-pref` → append to `settings/code.md`
- `meta` → **skip it**. `settings/meta.md` already describes 0.1.0; the old ones
  describe titles, summaries and refinement-on-demand, none of which exist.

Do not paste them in one bullet per node. They accumulated append-only and
several restate each other — **merge them into coherent sections**. This is where
the autoload budget is actually won: on the reference store, consolidating these
files is the difference between ~19k and ~8k characters loaded every session.

`settings/conv.md` is preloaded every session, so keep it tight. `settings/code.md`
is not preloaded — it is reached through the `code-prefs` skill — so it can afford
to be longer.

## 4. Repeat, then finish

Work through the batches with increasing `--offset`. Check progress at any point
with `migration status`, which reports `written_so_far` from the new store.

When everything is across:

```bash
python -m claude_memory migration done
```

That clears the flag and the hooks stop asking. The old store is left exactly
where it is — it is the only copy of anything you chose not to carry, so deleting
it is the user's call, not yours. Tell them where it is and that they can remove
it whenever they like.

## 5. Verify

```bash
python -m claude_memory t1 --render
python -m claude_memory search "something you know should be in there"
```
