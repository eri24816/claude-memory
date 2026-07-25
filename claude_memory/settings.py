"""Prefs as files, not nodes.

`conv-pref`, `code-pref` and `meta` are rules, not history. A rule has no
"true since" — only a current form — so the node model's immutability bought
nothing here and cost the one operation that matters: consolidation. Thirteen
conv-prefs accumulated in four days of use, several of them restatements of
each other, because nodes can only be appended and superseded, never merged.
A file can be rewritten in place, so overlapping rules collapse into one.

Deliberately NOT under ~/.claude/: that directory belongs to the Claude Code
harness, and these files are this project's data. They live in the repo, which
means a fresh clone starts without them — CLAUDE_MEMORY_SETTINGS is the escape
hatch for anyone who wants them somewhere durable.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

# Stripped before a file reaches T1. These files are edited by hand and by the
# agent, so they accumulate editorial notes -- "consolidate this", "why this
# rule exists" -- that are worth keeping in the file and not worth paying for on
# every single session start.
_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)

# One file per rule kind. The names are the node types they replaced, minus the
# `-pref` suffix that only existed to disambiguate them inside a shared table.
FILES = {
    "conv": "How the agent should communicate and behave",
    "code": "Coding constraints and conventions",
    "meta": "How this memory system works",
}

# Only these two are autoloaded. `code` is reached through a skill instead: it is
# the largest of the three and irrelevant to every session that writes no code,
# so paying for it on every session start is waste.
#
# The risk is real and this codebase has already measured it once -- raw nodes
# were meant to be refined on demand and were refined exactly zero times in 518
# opportunities, because a hint competes with the task and the task wins. What
# makes this different is not hope: `meta` is preloaded and carries the trigger
# line, so the instruction to load code conventions is in context before any
# code is written, rather than waiting to be discovered.
PRELOADED = ("meta", "conv")

# The node types that no longer belong in the store at all. store.remember()
# rejects these by name and points here, because a silent second home for the
# same rule is exactly the drift this move was meant to end.
RETIRED_TYPES = frozenset({"conv-pref", "code-pref", "meta"})

DEFAULT_SETTINGS_DIR = Path(__file__).resolve().parent.parent / "settings"


def settings_dir() -> Path:
    return Path(os.environ.get("CLAUDE_MEMORY_SETTINGS") or DEFAULT_SETTINGS_DIR)


def path_for(name: str) -> Path:
    if name not in FILES:
        raise KeyError(f"unknown settings file {name!r}; expected one of {sorted(FILES)}")
    return settings_dir() / f"{name}.md"


def read(name: str) -> str:
    """The file's text, or empty if it was never written.

    Missing is normal, not an error: migration only creates a file for a user
    who already had nodes of that kind, and a fresh install has none.
    """
    try:
        return path_for(name).read_text(encoding="utf-8-sig").strip()
    except (OSError, KeyError):
        return ""


def write(name: str, text: str) -> Path:
    path = path_for(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text.rstrip() + "\n", encoding="utf-8")
    return path


def render() -> str:
    """The preloaded prefs block for T1, in a fixed order so the text is stable.

    Rendered whole, never compressed. These are the parts of memory whose value
    is being present verbatim on turn one: a behavioural rule the agent has to
    go and fetch is a rule it will not follow. Facts pay the 8-word budget
    precisely so these do not have to.
    """
    blocks: list[str] = []
    for name in PRELOADED:
        text = _COMMENT.sub("", read(name)).strip()
        text = re.sub(r"\n{3,}", "\n\n", text)
        if text:
            blocks.append(text)
    return "\n\n".join(blocks)
