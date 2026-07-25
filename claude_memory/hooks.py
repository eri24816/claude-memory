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

# Matches README.md's `maintain_every`. A model cannot reliably count its own
# turns, and a meta instruction to "capture actively" alone loses to task load
# over a long conversation -- so the count lives here, forced by the hook,
# rather than delegated to self-reporting.
MAINTAIN_EVERY = 5

# A single holistic question ("is anything worth capturing?") anchors on
# whatever is most salient -- usually "has a decision been reached" -- and
# silently drops everything else competing for attention. A checklist forces
# each category to be considered on its own rather than folded into one
# judgment call; that gap is exactly what let three sourced facts and two
# capture-worthy user actions pass a real "nothing new qualifies" check.
MAINTENANCE_REMINDER = (
    "This is turn {count} since this session started. Before continuing, "
    "check each of these separately -- do not let 'no decision yet' answer "
    "for all of them:\n"
    "1. A correction, stated preference, or change in Eric's situation.\n"
    "2. A fact or finding you established through research or verification, "
    "even with no decision reached -- a finding does not need a decision to "
    "be worth saving.\n"
    "3. A decision Eric actually made, with the reason it won.\n"
    "4. A request from Eric that triggered real work (research, comparison, "
    "verification) -- worth an action node of its own, separate from what "
    "the work produced.\n"
    "Note the two write paths: a stated preference, correction or convention is "
    "an EDIT to settings/conv.md or settings/code.md -- consolidate it into what "
    "is already there rather than appending a near-duplicate. Everything else is "
    "`remember`.\n"
    "If any of the four clears the bar, load the memory skill and write it "
    "now. If none do, say so in one line and continue."
)

# Emitted by SessionStart and UserPromptSubmit while a pre-0.1.0 store is still
# waiting to be carried across. Once per message, not once per session: a session
# that started before the flag was set would otherwise go on treating memory as
# empty, which from the inside is indistinguishable from a user who has never
# said anything.
#
# NOT emitted from Stop. Stop's additionalContext forces the turn to continue, so
# anything unconditional there makes the conversation unable to end, and
# UserPromptSubmit already covers every path into a turn.
MIGRATION_NOTICE = (
    "MEMORY IS MID-MIGRATION. A pre-0.1.0 store with {total} nodes is at {legacy} "
    "and has not been carried into the new store yet. Memory is NOT empty -- the "
    "old nodes are simply not here, so do not conclude that anything was never "
    "saved.\n"
    "Ask the user whether to migrate now. If they agree, follow {doc}. The old "
    "store is only ever read, so this is safe to start and safe to stop halfway."
)

# A different failure with the same symptom: the configured store IS the old one,
# because CLAUDE_MEMORY_DB points at it. Nothing can be done from inside that
# store, so the notice says where to point instead.
LEGACY_TARGET_NOTICE = (
    "MEMORY IS MISCONFIGURED. The store at {db} is a pre-0.1.0 store, and this "
    "code cannot read or write it. Unset CLAUDE_MEMORY_DB (or point it at a new "
    "path) so a current store is created, then follow {doc} to carry the old "
    "nodes across."
)


def _doc_path() -> Path:
    return Path(__file__).resolve().parent.parent / "MIGRATION-0.1.0.md"


def migration_notice(connection: sqlite3.Connection) -> str:
    """The notice, or empty if memory is ready to use."""
    from . import db, migration

    columns = {row[1] for row in connection.execute("PRAGMA table_info(nodes)")}
    if columns and "claim" not in columns:
        attached = connection.execute("PRAGMA database_list").fetchone()
        return LEGACY_TARGET_NOTICE.format(
            db=attached["file"] if attached and attached["file"] else db.DEFAULT_DB_PATH,
            doc=_doc_path(),
        )

    if not migration.is_migrating():
        return ""

    state = migration.read_state()
    return MIGRATION_NOTICE.format(
        total=state.get("legacy_total", "some"),
        legacy=state.get("legacy_db", db.LEGACY_DB_PATH),
        doc=_doc_path(),
    )


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

    notice = migration_notice(connection)
    if notice:
        # Returned alone. A half-converted store renders claims that are still
        # NULL and facts that may already have been deleted, and a stale autoload
        # block that looks normal is worse than none: the agent would act on it.
        return notice

    scope = scope_for_cwd(payload.get("cwd") or os.getcwd())
    return retrieval.render_t1(retrieval.assemble_t1(connection, scope=scope))


def build_user_prompt_context(payload: dict, connection: sqlite3.Connection) -> str:
    """The per-message retrieval block, given an already-open connection.

    The gate matters more than the ranking here: this fires on every message,
    including "ok" and "yes", and injecting three unrelated nodes into a
    message that needed none is worse than injecting nothing.
    """
    from . import retrieval

    # Checked before the should_retrieve gate, not after: the gate exists to
    # avoid injecting irrelevant nodes into "ok" and "yes", but a broken store
    # is not a relevance question. It is equally true on every message.
    notice = migration_notice(connection)
    if notice:
        return notice

    prompt = (payload.get("prompt") or "").strip()
    if not prompt or not retrieval.should_retrieve(prompt):
        return ""

    scope = scope_for_cwd(payload.get("cwd") or os.getcwd())
    hits = retrieval.search_stratified(
        connection, prompt, scope=scope,
        exclude_ids=retrieval.t1_ids(connection, scope=scope),
    )
    return retrieval.render_context(hits)


def _bump_stop_count(connection: sqlite3.Connection, session_id: str) -> int:
    from .models import now

    connection.execute(
        "INSERT INTO hook_state (session_id, stop_count, updated) VALUES (?, 1, ?) "
        "ON CONFLICT(session_id) DO UPDATE SET "
        "stop_count = stop_count + 1, updated = excluded.updated",
        (session_id, now()),
    )
    connection.commit()
    row = connection.execute(
        "SELECT stop_count FROM hook_state WHERE session_id = ?", (session_id,)
    ).fetchone()
    return row["stop_count"]


def build_stop_context(payload: dict, connection: sqlite3.Connection) -> str:
    """Every MAINTAIN_EVERY stops, remind the agent to check what is worth
    capturing from the stretch of conversation since the last check.

    Stays silent otherwise -- Stop's additionalContext forces the turn to
    continue even without decision:block, so emitting it on every stop would
    mean the conversation could never end.
    """
    session_id = payload.get("session_id")
    if not session_id:
        return ""

    count = _bump_stop_count(connection, session_id)
    if count % MAINTAIN_EVERY != 0:
        return ""

    # The migration notice deliberately does NOT go here. Stop's additionalContext
    # forces the turn to continue, so anything unconditional makes the
    # conversation unable to end -- and UserPromptSubmit already fires on every
    # message, which is every path into a turn. Nothing is lost by leaving Stop
    # alone, and the failure mode avoided is severe.
    #
    # What IS suppressed: the reminder itself, once the store is behind. Telling
    # an agent to capture what it learned is noise when every write would raise.
    if migration_notice(connection):
        return ""

    return MAINTENANCE_REMINDER.format(count=count)


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


def stop() -> int:
    """Force the every-N-turns memory check the meta node cannot rely on."""
    payload = _read_payload()

    from . import db

    connection = db.connect()
    try:
        block = build_stop_context(payload, connection)
    finally:
        connection.close()

    if block:
        _emit("Stop", block)
    return 0


COMMANDS = {
    "session-start": session_start,
    "user-prompt-submit": user_prompt_submit,
    "stop": stop,
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
