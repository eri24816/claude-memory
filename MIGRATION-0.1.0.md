# Migrating a store to v0.1.0

**If you are the user:** type `migrate to v0.1.0` in Claude Code. The agent
reads this file and does the rest. You do not need the details below.

**If you are the agent:** you are executing this. Read the whole file first.

---

## What changes

| v0 | v0.1.0 |
|---|---|
| `title` (unique) + `summary` (one sentence, in practice ~700 chars) | `claim` (≤ 8 words, rendered) + `detail` (nullable, only shown by `dig`) |
| `title` is the handle | `id` is the handle; `resolve` also accepts a claim or an unambiguous fragment |
| `conv-pref` / `code-pref` / `meta` are nodes | files in `settings/` — editable, so rules can be *merged* instead of only appended |
| `raw` wiki chunks, refined on demand | removed; returns in 0.2.0 with a per-query cap |
| store at `~/.claude/memory/memory.db` | store at `settings/memory.db`, beside the pref files |

## Before you start

**Stop every other Claude session.** They write to the same store. A node
written between the schema change and the last `--set` lands with `claim IS NULL`
and silently joins the pending set — recoverable, but confusing. `claude agents
--json` lists the running ones.

Migration takes a backup automatically (`memory.pre-0.1.0.db`) and copies rather
than moves the legacy store, so the original at `~/.claude/memory/` stays valid
until the user deletes it themselves. `python -m claude_memory migrate --rollback`
restores the backup.

## Step 1 — the mechanical part

```bash
python -m claude_memory migrate
```

This relocates the store, backs it up, extracts the pref nodes into
`settings/conv.md` and `settings/code.md`, replaces `settings/meta.md` with the
v1 template, drops the `raw` nodes, converts the schema, and reports how many
nodes are waiting for a claim. It is idempotent — running it twice is safe.

`meta.md` is *replaced*, not migrated: every v0 meta node describes titles,
summaries and refinement-on-demand, none of which exist now.

## Step 2 — write the claims (this is the part that needs you)

```bash
python -m claude_memory migrate --next 20
```

Returns nodes with their old `title` and `detail`. For each one write a claim,
then submit:

```bash
python -m claude_memory migrate --set --file claims.json
```

```json
[
  {"id": "apartment", "claim": "Eric's apartment is 2442 Leslie Circle"},
  {"id": "discovery", "claim": "Eric will apply for Discovery card",
   "detail": null}
]
```

Repeat until `pending` reaches 0. It finalizes itself at that point — drops
`title`, rebuilds both indexes, stamps the version. **Do not try to finish in one
context.** The cursor is `claim IS NULL`; stopping and resuming tomorrow costs
nothing.

### Writing a good claim

- **Eight words maximum**, enforced. Compress by deleting function words, not by
  truncating: *"Eric will apply for Discovery card"*, not *"After arriving in
  Ann Arbor, Eric will apply for the…"*.
- **No dates.** `window_start`/`window_end` already render as `[2026-08-06..]`.
  A date in the claim wastes a word and will drift from the field.
- **Front-load the distinguishing term.** The claim is what search returns and
  what `resolve` matches on.
- **`detail` is usually null.** Omit the key to keep the old summary as-is; pass
  `null` to drop it. Keep detail only for a verification trail, a gotcha, or a
  correction — if you preserve every old summary wholesale, you have moved the
  bloat rather than removed it.

## Step 3 — consolidate the pref files

**The size win lives here, not in step 2.** Migration dumps the old pref nodes
verbatim, one bullet each, because nodes could only be appended — several of them
restate each other. Rewrite each file into coherent sections, merging duplicates.
On the reference store this is the difference between ~19k and ~8k characters of
autoload.

- `settings/conv.md` — preloaded every session. Keep it tight.
- `settings/code.md` — **not** preloaded; reached through the `code-prefs` skill.
- `settings/meta.md` — already the v1 template. Leave it unless the workflow
  genuinely differs.

HTML comments are stripped before these files reach context, so editorial notes
cost nothing.

## Step 4 — verify

```bash
python -m claude_memory migrate --status     # expect: up to date, version 1
python -m claude_memory t1 --render          # sanity-check the autoload block
python -m claude_memory search "something you know is in there"
```

Then tell the user the old store is still at `~/.claude/memory/memory.db` and can
be deleted once they are satisfied.
