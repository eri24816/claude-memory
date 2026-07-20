"""Hook entry points.

    python -m claude_memory.hooks session-start

Every hook fails silently by design: a memory problem must never stop a session
from starting. Set CLAUDE_MEMORY_DEBUG=1 to surface errors on stderr instead.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any

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


def build_session_start_context(payload: dict, connection: sqlite3.Connection) -> str:
    """The T1 autoload block, given an already-open connection.

    Split out from session_start() so the same logic is reachable from a test
    or (in principle) the daemon without a fresh process per call -- session
    start itself is pure SQL, so it does not need the daemon for speed, but
    keeping this symmetric with build_user_prompt_context avoids two shapes
    for the same kind of thing.
    """
    from . import retrieval

    scope = scope_for_cwd(payload.get("cwd") or os.getcwd())
    return retrieval.render_t1(retrieval.assemble_t1(connection, scope=scope))


def build_user_prompt_context(payload: dict, connection: sqlite3.Connection) -> str:
    """The per-message retrieval block, given an already-open connection.

    The gate matters more than the ranking here: this fires on every message,
    including "ok" and "yes", and injecting three unrelated nodes into a
    message that needed none is worse than injecting nothing.
    """
    from . import retrieval

    prompt = (payload.get("prompt") or "").strip()
    if not prompt or not retrieval.should_retrieve(prompt):
        return ""

    scope = scope_for_cwd(payload.get("cwd") or os.getcwd())
    hits = retrieval.search_stratified(
        connection, prompt, scope=scope,
        exclude_ids=retrieval.t1_ids(connection, scope=scope),
    )
    return retrieval.render_context(hits)


def _emit(event_name: str, block: str) -> None:
    json.dump(
        {"hookSpecificOutput": {"hookEventName": event_name, "additionalContext": block}},
        sys.stdout,
    )
    sys.stdout.write("\n")


def session_start() -> int:
    """Emit the T1 autoload block, and kick off the daemon in the background.

    Assembling T1 never needed the daemon -- it is pure SQL -- but this is the
    one guaranteed moment before the first message, so it is the right place
    to start warming the embedding model for user_prompt_submit(). Eric reads
    the autoloaded block for a few seconds at minimum; that is free warmup time
    that would otherwise be spent idle.
    """
    payload = _read_payload()

    from . import daemon, db

    connection = db.connect()
    try:
        block = build_session_start_context(payload, connection)
    finally:
        connection.close()

    try:
        daemon.ensure_running()
    except OSError:
        pass  # warmup is an optimization, never a requirement

    if block:
        _emit("SessionStart", block)
    return 0


def user_prompt_submit() -> int:
    """Retrieve for the message the user just sent, and inject it alongside.

    Tries the warm daemon first -- fastembed's cold start is about 1.2s paid
    fresh in every argv process, and this hook fires on every message. Falls
    back to doing the work in-process if nothing answers, so a message is never
    silently dropped just because the daemon has not started yet.
    """
    payload = _read_payload()

    from . import daemon

    result = daemon.request("user-prompt-submit", payload)
    if result is not None:
        block = result.get("additionalContext", "")
    else:
        from . import db

        connection = db.connect()
        try:
            block = build_user_prompt_context(payload, connection)
        finally:
            connection.close()
        try:
            daemon.ensure_running()
        except OSError:
            pass

    if block:
        _emit("UserPromptSubmit", block)
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
