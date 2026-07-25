# Installing claude-memory on your own machine

This sets up a persistent, cross-session memory for Claude Code: an autoloaded
core (T1) plus hybrid BM25 + vector search over a private SQLite store, wired in
through three Claude Code hooks. See [README.md](README.md) for the design; this
file is just how to get it running.

The whole thing runs locally. Nothing leaves your machine — the embedding model
runs on CPU, and the store is a SQLite file in your home directory.

---

## 0. Prerequisites

- **Python 3.11 or newer** (`python --version`).
- **Claude Code** installed and working.
- **A Python whose SQLite can load extensions.** `sqlite-vec` is a loadable
  extension, and a few Python builds ship without extension support (notably the
  system Python on some macOS setups). Step 2 verifies this; if it fails there's a
  one-line fix.
- **Git**, to clone.

> **Do not use the `dev.db` in the repo.** It is my personal memory store and is
> gitignored anyway. You start empty in step 3.

---

## 1. Clone and install

```bash
git clone https://github.com/eri24816/claude-memory.git
cd claude-memory

# A virtualenv is recommended but read the PATH warning in step 4 first —
# the hooks call bare `python`, so it must resolve to the env you install into.
python -m pip install -e .
```

This pulls the two runtime dependencies: `sqlite-vec` (the vector index) and
`fastembed` (the embedding model, CPU, no PyTorch).

The first time anything computes an embedding, `fastembed` downloads the
`bge-small-en-v1.5` model (~90 MB) into a local cache. That happens once.

---

## 2. Verify your SQLite can do what this needs

```bash
python scripts/check_env.py
```

Expected output — a `sqlite_vec` version, `fts5: True`, a `bm25 score`, and a
`knn` result. If instead you get an error about `enable_load_extension` or
"cannot load extensions", your Python build can't load them. Fix it by installing
a bundled SQLite:

```bash
python -m pip install pysqlite3-binary
```

(On most Windows and Linux Pythons this just works and you won't need it.)

---

## 3. Create your (empty) store

```bash
python -m claude_memory init
```

This creates the SQLite store and applies the schema. By default it lives in the
repo, beside the preference files:

```
<repo>/settings/memory.db
```

`settings/` holds everything you own — the store plus `conv.md`, `code.md` and
`meta.md` — and is gitignored whole, so nothing ships as a default and a `git
pull` can never conflict with a rule you wrote. It is deliberately **not** under
`~/.claude/`, which belongs to the Claude Code harness rather than to this
project.

To put it elsewhere, set these **before** running any command — the hooks read
the same variables, so set them in your shell profile if you override them:

```bash
export CLAUDE_MEMORY_SETTINGS="/path/to/settings"   # store + pref files
export CLAUDE_MEMORY_DB="/path/to/memory.db"        # just the store
```

```powershell
$env:CLAUDE_MEMORY_SETTINGS = "C:\path\to\settings"
```

**Upgrading an existing store?** If you already have one at
`~/.claude/memory/memory.db` from before 0.1.0, do not run `init` — type
`migrate to v0.1.0` in Claude Code, or see
[MIGRATION-0.1.0.md](MIGRATION-0.1.0.md). Migration copies the old store to the
new location rather than moving it, so the original stays valid until you delete
it yourself.

---

## 4. Wire up the three hooks

The system is driven entirely by Claude Code hooks in your **global** settings
file, `~/.claude/settings.json`. Add this `"hooks"` block (merge it in if the file
already exists — don't clobber your other settings):

```json
{
  "hooks": {
    "SessionStart": [
      { "hooks": [ { "type": "command", "command": "python",
        "args": ["-m", "claude_memory.hooks", "session-start"],
        "timeout": 20, "statusMessage": "Loading memory" } ] }
    ],
    "UserPromptSubmit": [
      { "hooks": [ { "type": "command", "command": "python",
        "args": ["-m", "claude_memory.hooks", "user-prompt-submit"],
        "timeout": 15, "statusMessage": "Searching memory" } ] }
    ],
    "Stop": [
      { "hooks": [ { "type": "command", "command": "python",
        "args": ["-m", "claude_memory.hooks", "stop"],
        "timeout": 10, "statusMessage": "Checking memory" } ] }
    ]
  }
}
```

What each one does:

| Hook | Fires | Job |
|---|---|---|
| `SessionStart` | new session / after compaction | inject the T1 autoload block |
| `UserPromptSubmit` | every message | search memory for the message, inject relevant hits |
| `Stop` | end of each turn | every 5 turns, remind the agent to capture what's worth saving |

> ### ⚠️ The `python` PATH gotcha — the #1 reason this silently does nothing
> The hooks run `python` from your PATH, in a plain environment. If you installed
> into a **virtualenv**, a bare `python` in the hook won't be that env's Python and
> `import claude_memory` will fail — and hooks **fail silently by design** (a
> memory glitch must never block a session). Two safe options:
> - Install into the Python that a bare `python` actually resolves to (check with
>   `which python` / `where python`), **or**
> - Replace `"command": "python"` with the **absolute path** to your venv's Python
>   (e.g. `"/home/you/claude-memory/.venv/bin/python"` or
>   `"C:\\...\\.venv\\Scripts\\python.exe"`).
>
> To debug, set `CLAUDE_MEMORY_DEBUG=1` and the hooks will print errors to stderr
> instead of swallowing them.

After editing, validate the SessionStart hook is well-formed:

```bash
python scripts/validate_hook_config.py
```

---

## 5. Seed the meta node (and make it *yours*)

The **meta** node is the one node whose whole job is to be loaded first — it tells
the agent how to read, write, and correct memory. Without it, the agent sees
remembered facts but no instructions for using them. Seed it:

```bash
python scripts/seed_meta.py
python scripts/seed_prefs.py   # optional: some starter conv/code preferences
```

> **These scripts are written for me, by name ("Eric"), with my conventions.**
> Before seeding, open `scripts/seed_meta.py` and `scripts/seed_prefs.py` and
> replace the personal bits — your name in the `SUMMARY` text, and any preference
> that isn't yours. Otherwise your Claude will think it's remembering for me. The
> node *structure* is what you want; the *content* you should make your own.

---

## 6. Install the `memory` skill (recommended)

The meta node tells the agent to "load the `memory` skill" when writing memory —
that skill is the field schema and the supersede/retire rules. Make it
discoverable by copying it where Claude Code looks for skills:

```bash
# global skills dir
mkdir -p ~/.claude/skills
cp -r skills/memory ~/.claude/skills/memory
```

(Or symlink it if you'd rather track the repo copy.)

---

## 7. Optional: start the warm-embedding daemon

Every message embeds the query. A background daemon keeps the model warm so you
don't pay the ~1.2s cold start each time. `SessionStart` starts it for you
fire-and-forget, but you can manage it directly:

```bash
python -m claude_memory daemon start
python -m claude_memory daemon status
python -m claude_memory daemon stop
```

It's optional — if the daemon isn't up, the hook computes in-process and nothing
is dropped, just a little slower.

---

## 8. Verify end to end

```bash
# Write a test node…
python -m claude_memory remember --json '[{"claim":"claude-memory install smoke test","type":"fact","about_user":true}]'

# …search for it…
python -m claude_memory search "install test"

# …and read the T1 autoload block a fresh session would see.
python -m claude_memory t1 --render
```

> On Windows PowerShell, inline JSON is fragile — prefer `--file nodes.json`
> (UTF-8) over `--json '...'`.

Then start a **new** Claude Code session in any project. You should see "Loading
memory" at startup and your seeded prefs/meta take effect. Ask it to remember
something, end the turn, and confirm it lands via `search`.

---

## Everyday commands

```bash
python -m claude_memory search "<query>" "<another angle>"   # batched, cheaper
python -m claude_memory dig <id-or-claim> <another>          # nodes in full
python -m claude_memory t1 --render             # the autoload set
python -m claude_memory where                   # resolved store + settings paths
python -m claude_memory snapshot backup.db      # consistent backup (VACUUM INTO)
python -m claude_memory stale "<handle>"        # mark a node no longer true
python -m claude_memory migrate --status        # schema version
```

`search` and `dig` take several arguments per call and should be used that way: a
tool call is billed for its cached prefix once, so three queries in one call cost
a fraction of three separate calls.

Coding conventions are **not** autoloaded — they live in `settings/code.md` and
are reached through the `code-prefs` skill.

The store is precious and not diffable — **back it up with `snapshot`, never
commit the `.db`** (it's gitignored for that reason).

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Nothing happens on session start | hook `python` ≠ install env | use absolute venv python path (step 4) |
| `check_env.py` errors on extensions | Python can't load SQLite extensions | `pip install pysqlite3-binary` |
| `ModuleNotFoundError: claude_memory` | not installed in that Python | `pip install -e .` in the right env |
| Hooks do nothing, no error | they fail silently by design | run with `CLAUDE_MEMORY_DEBUG=1` |
| `UnicodeEncodeError` on Windows | console codepage | already handled by the CLI; use `--file` for input |
| Agent talks about "Eric" | seed content not customized | edit `scripts/seed_meta.py` (step 5) |
