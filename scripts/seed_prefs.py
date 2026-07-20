"""Migrate the global CLAUDE.md rules into nodes, so the file can be retired.

CLAUDE.md is loaded verbatim into every session of every project. That is the
behaviour T1 replaces — but only for rules that actually exist as nodes, and an
audit found nine that did not. This script creates them before the file goes.

The memory section is deliberately not copied: it describes routing into
CLAUDE.md and per-project memory folders, which is the mechanism being replaced.
Its durable half — capture actively, and what counts as worth capturing — folds
into the meta node instead, via seed_meta.py.

Coding rules land as `code-pref`, which is *not* autoloaded: the meta node tells
the agent to read them before writing code. Rules about how to talk to Eric or
which tools to reach for are `conv-pref`, which is.
"""

import sys

from claude_memory import connect
from claude_memory.store import remember

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PREFS = [
    {
        "id": "chrome-for-web-interfaces",
        "title": "Test web interfaces through Chrome",
        "summary": "When a project has a web interface, Eric expects the agent to "
                   "drive it with the Chrome MCP tools (mcp__claude-in-chrome__*) "
                   "— navigate to the running app, exercise the features, and "
                   "report bugs found — rather than reasoning about the UI from "
                   "source alone.",
        "type": "conv-pref",
    },
    {
        "id": "chrome-for-web-search",
        "title": "Search the web through Chrome",
        "summary": "For web searches Eric prefers Chrome browser automation "
                   "(mcp__claude-in-chrome__*) driving a Google search in a new "
                   "tab, falling back to the WebSearch/WebFetch tools only when "
                   "Chrome is not connected.",
        "type": "conv-pref",
    },
    {
        "id": "math-formatting",
        "title": "How to format math for Eric's viewer",
        "summary": "Write display math in $$...$$ with LaTeX inside, which is what "
                   "typesets in Eric's viewer. For short inline math use plain "
                   "Unicode (z = f(g), Δt, Σ, ≈, ≠, cᵢ); inline $...$ and \\(...\\) "
                   "render as literal text. Reserve untagged code fences for code.",
        "type": "conv-pref",
    },
    {
        "id": "vite-typecheck-command",
        "title": "Typechecking a Vite project",
        "summary": "In Vite projects run `tsc -b --noEmit`, never plain "
                   "`tsc --noEmit`: the react-ts template splits tsconfig via "
                   "`references`, so the plain form silently checks nothing "
                   "(vitejs/vite#17585).",
        "type": "code-pref",
    },
    {
        "id": "descriptive-variable-names",
        "title": "Descriptive variable names",
        "summary": "Use clear, spelled-out variable names, never cryptic two-letter "
                   "abbreviations: `feasible_bins` not `feas`, `crop_batch` not "
                   "`cb`, `dt_loss_mask` not `dm`. Single-letter loop indices "
                   "(i, b, k) and conventional math coordinates (x, y) remain fine.",
        "type": "code-pref",
    },
    {
        "id": "four-space-indentation",
        "title": "Four-space indentation",
        "summary": "Prefer 4-space indentation in all code Eric's projects.",
        "type": "code-pref",
    },
    {
        "id": "commit-before-training-run",
        "title": "Commit before launching a training run",
        "summary": "Before starting a new training version or run, commit the code "
                   "changes that define it, so every run maps to an exact "
                   "recoverable code state. Commit first, then launch.",
        "type": "code-pref",
    },
    {
        "id": "prefer-change-and-resume",
        "title": "Change-and-resume over change-and-restart",
        "summary": "When changing a training setting, prefer change-and-resume "
                   "whenever the run is checkpointed and the change can apply "
                   "mid-run — bumping a KL weight, tweaking a loss term, altering "
                   "logging or visualisations. Save a checkpoint, apply the change, "
                   "resume. Only restart from scratch when the change invalidates "
                   "mid-run state: architecture or parameter-count changes, or "
                   "anything that breaks optimizer state or the data pipeline.",
        "type": "code-pref",
    },
]

connection = connect()
existing = {
    row["id"]
    for row in connection.execute("SELECT id FROM nodes")
}
fresh = [spec for spec in PREFS if spec["id"] not in existing]

if fresh:
    remember(connection, fresh)
for spec in PREFS:
    mark = "+" if spec["id"] in {s["id"] for s in fresh} else "="
    print(f"  {mark} [{spec['type']}] {spec['id']}")
print(f"\n{len(fresh)} added, {len(PREFS) - len(fresh)} already present")
connection.close()
