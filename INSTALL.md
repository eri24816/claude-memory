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

## 3. Create your (empty) store and settings

```bash
python -m claude_memory init
```

This does three things, and re-running it is always safe:

- creates the SQLite store and applies the schema;
- creates empty `settings/conv.md` and `settings/code.md` for your own rules;
- links `skills/memory` and `skills/code-prefs` into `~/.claude/skills`.

The third one is the part that is easy to skip and impossible to notice: hooks
fail silently, and a missing skill just means the agent quietly uses the wrong
schema. Run `python -m claude_memory install` on its own whenever you want just
that part again.

The store lives in the repo by default, beside the preference files:

```
<repo>/settings/memory.db
```

`settings/` holds everything you own — the store plus `conv.md` and `code.md` —
and is gitignored whole, so nothing ships as a default and a `git pull` can never
conflict with a rule you wrote. It is deliberately **not** under `~/.claude/`,
which belongs to the Claude Code harness rather than to this project.

`meta.md` is the exception: it sits at the repo root and **is** tracked. It
describes how the memory system works rather than anything about you, so it
should arrive with the clone and update with a `git pull`. It is also what tells
the agent this system exists at all — without it the agent falls back to the
file-based memory in its system prompt and writes markdown into a directory
nothing here reads.

To put it elsewhere, set these **before** running any command — the hooks read
the same variables, so set them in your shell profile if you override them:

```bash
export CLAUDE_MEMORY_SETTINGS="/path/to/settings"   # store + pref files
export CLAUDE_MEMORY_DB="/path/to/memory.db"        # just the store
```

```powershell
$env:CLAUDE_MEMORY_SETTINGS = "C:\path\to\settings"
```

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

## 5. Make the rule files yours

Three files hold rules, and nothing ships as a default preference, so the two
that are yours start empty:

| File | Tracked | Loaded | What goes in it |
|---|---|---|---|
| `meta.md` (repo root) | yes | every session, whole | how this memory system works — edit only if you change the system |
| `settings/conv.md` | no | every session, whole | how the agent should communicate and behave |
| `settings/code.md` | no | via the `code-prefs` skill | build/test invocations, environment quirks, style rules |

You don't have to write anything now: the agent edits these itself when you state
a preference or correct it. The one rule is **merge, don't append** — that is the
whole reason these are files rather than nodes.

`conv.md` is paid for on every session start, so keep it tight. `code.md` is not
preloaded, so it can afford to be longer.

---

## 6. Skills

Step 3 already linked both skills into `~/.claude/skills`:

- **`memory`** — the field schema and the supersede/retire rules, loaded before
  writing a node.
- **`code-prefs`** — points at `settings/code.md`, loaded before writing code.

They are **links**, not copies, so a `git pull` reaches the live skill
immediately — a symlink where the platform allows one, a directory junction on
Windows, which needs no Developer Mode or elevation. If neither is possible
`install` copies instead and reports `"linked": false`; in that case re-run
`python -m claude_memory install` after any change under `skills/`, or the
installed copy will go on teaching an outdated schema.

Verify with `/skills` in Claude Code, or:

```bash
python -m claude_memory install     # idempotent; reports linked vs copied
```

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
python -m claude_memory install                 # re-link skills, seed missing settings
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
| Agent ignores memory, writes `~/.claude/memory/*.md` | `meta.md` empty or missing | restore it from git |
| Agent uses an old field schema (`title`/`summary`) | installed skill is a stale copy | `python -m claude_memory install` |
