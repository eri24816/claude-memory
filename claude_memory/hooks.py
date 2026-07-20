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


def user_prompt_submit() -> int:
    """Retrieve for the message the user just sent, and inject it alongside.

    The gate matters more than the ranking here: this fires on every message,
    including "ok" and "yes", and injecting three unrelated nodes into a message
    that needed none is worse than injecting nothing.
    """
    payload = _read_payload()
    prompt = (payload.get("prompt") or "").strip()
    if not prompt:
        return 0

    from . import db, retrieval

    if not retrieval.should_retrieve(prompt):
        return 0

    scope = scope_for_cwd(payload.get("cwd") or os.getcwd())
    connection = db.connect()
    try:
        hits = retrieval.search_stratified(
            connection, prompt, scope=scope,
            exclude_ids=retrieval.t1_ids(connection, scope=scope),
        )
    finally:
        connection.close()

    block = retrieval.render_context(hits)
    if not block:
        return 0

    json.dump(
        {
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": block,
            }
        },
        sys.stdout,
    )
    sys.stdout.write("\n")
    return 0


COMMANDS = {
    "session-start": session_start,
    "user-prompt-submit": user_prompt_submit,
}


def main(argv: list[str] | None = None) -> int:
    # json.dump escapes non-ASCII by default, so this is belt-and-braces today.
    # It matters because the failure mode here is silent: main() swallows every
    # exception, so an encoding error would drop the whole memory block rather
    # than announce itself.
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")

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
