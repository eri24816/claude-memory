"""Hook entry points.

    python -m claude_memory.hooks session-start

Every hook fails silently by design: a memory problem must never stop a session
from starting. Set CLAUDE_MEMORY_DEBUG=1 to surface errors on stderr instead.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

SCOPE_PREFIX = "project:"


def scope_for_cwd(cwd: str | None) -> str:
    """Derive a stable project scope from a working directory.

    Global-scope nodes load everywhere; this only adds the current project's.
    """
    if not cwd:
        return "global"
    path = Path(cwd)
    name = path.name or path.drive.replace(":", "").lower() or "root"
    return f"{SCOPE_PREFIX}{name}"


def _read_payload() -> dict:
    if sys.stdin is None or sys.stdin.closed:
        return {}
    try:
        raw = sys.stdin.read()
    except OSError:
        return {}
    if not raw.strip():
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


def session_start() -> int:
    """Emit the T1 autoload block as additionalContext."""
    payload = _read_payload()
    scope = scope_for_cwd(payload.get("cwd") or os.getcwd())

    from . import db, retrieval

    connection = db.connect()
    try:
        nodes = retrieval.assemble_t1(connection, scope=scope)
    finally:
        connection.close()

    block = retrieval.render_t1(nodes)
    if not block:
        return 0

    json.dump(
        {
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": block,
            }
        },
        sys.stdout,
    )
    sys.stdout.write("\n")
    return 0


COMMANDS = {"session-start": session_start}


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if not arguments or arguments[0] not in COMMANDS:
        return 0

    try:
        return COMMANDS[arguments[0]]()
    except Exception as error:  # noqa: BLE001 - a hook must never break a session
        if os.environ.get("CLAUDE_MEMORY_DEBUG"):
            print(f"claude-memory hook failed: {error!r}", file=sys.stderr)
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
