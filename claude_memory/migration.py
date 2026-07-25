"""Carrying a pre-0.1.0 store forward.

The whole design is: **never touch the old store**. It is opened read-only, read
by an agent, and left exactly where it is. Nodes are re-written into the new
store through `remember`, the same path every ordinary capture takes.

That deletes almost everything a migration usually needs. No ALTER TABLE, so no
half-applied schema. No relocation, so no file being replaced under a live
connection. No backup or rollback, because the old store *is* the backup and is
never written. No resumable cursor, because the new store's own contents are the
progress -- an interrupted migration resumes by looking at what is already there.

What remains is a flag and a reading list:

    0. A hook finds no store, so one is created.
    1. If a legacy store exists, mark `migrating` and tell the agent.
    2. Stop the old daemon, which is still pointed at the old store.
    3. The agent reads the old nodes and re-writes them with the CLI.
    4. `migration done` clears the flag.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

from . import db, settings

STATE_FILE = "migration.json"

# Types that stopped being nodes in 0.1.0. Listed so the agent is told to route
# them into settings files rather than silently dropping them, and so the counts
# it is working through are honest.
PREF_TYPES = {"conv-pref": "conv.md", "code-pref": "code.md", "meta": "meta.md"}


def state_path() -> Path:
    return settings.settings_dir() / STATE_FILE


def read_state() -> dict[str, Any]:
    try:
        return json.loads(state_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _write_state(state: dict[str, Any]) -> None:
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2), encoding="utf-8")


def is_migrating() -> bool:
    return bool(read_state().get("migrating"))


def _open_legacy(path: Path | None = None) -> sqlite3.Connection | None:
    """Read-only handle on the old store, or None if there is nothing usable.

    `mode=ro` is the enforcement, not a hint: it makes "we never write to the old
    store" a property of the connection rather than a promise in a docstring.
    """
    path = Path(path or db.LEGACY_DB_PATH)
    if not path.exists():
        return None
    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        connection.execute("SELECT 1 FROM nodes LIMIT 1")
        return connection
    except sqlite3.DatabaseError:
        return None


def legacy_counts(path: Path | None = None) -> dict[str, int]:
    """How much is waiting, by type. Empty if there is no legacy store."""
    connection = _open_legacy(path)
    if connection is None:
        return {}
    try:
        return {
            row["type"]: row["n"]
            for row in connection.execute(
                "SELECT type, COUNT(*) AS n FROM nodes "
                "WHERE superseded_by IS NULL GROUP BY type"
            )
        }
    finally:
        connection.close()


def _stop_legacy_daemon() -> int | None:
    """Kill the daemon that belongs to the OLD store.

    It is the one process that outlives the upgrade: sessions reach memory
    through short-lived hook processes that pick up new code for free, while the
    daemon holds whatever it imported at start-up and answers from the store it
    was pointed at. Its registration file lives beside that store, not beside the
    new one, so the ordinary `daemon stop` looks in the wrong place.
    """
    discovery = db.LEGACY_DB_PATH.parent / "daemon.json"
    try:
        info = json.loads(discovery.read_text(encoding="utf-8"))
        pid = int(info["pid"])
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None

    if not _is_daemon_process(pid):
        # The discovery file outlives the process that wrote it -- nothing
        # deletes it on a crash or a reboot -- and operating systems reuse pids.
        # Trusting a stale file means killing whatever unrelated program now
        # holds that number. Verify what the pid actually is, and if that cannot
        # be established, leave it alone: a surviving stale daemon is a nuisance,
        # killing someone's editor is not.
        return None

    try:
        from .daemon import _terminate

        _terminate(pid)
    except Exception:  # pragma: no cover - defensive; a stale reader is survivable
        return None
    return pid


def _is_daemon_process(pid: int) -> bool:
    """True only if `pid` is demonstrably a claude_memory daemon."""
    try:
        import subprocess

        if sys.platform == "win32":
            result = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 f"(Get-CimInstance Win32_Process -Filter 'ProcessId={pid}')"
                 ".CommandLine"],
                capture_output=True, text=True, timeout=15,
            )
        else:
            result = subprocess.run(
                ["ps", "-p", str(pid), "-o", "args="],
                capture_output=True, text=True, timeout=15,
            )
        return "claude_memory" in result.stdout and "daemon" in result.stdout
    except Exception:
        return False


def begin_if_needed(path: Path | None = None) -> dict[str, Any]:
    """Called when a brand-new store is created. Flags a pending migration.

    Deliberately driven by store *creation* rather than by a version check. A
    version mismatch can only be seen by opening the old store as if it were the
    current one, which is what made the previous design fragile; "there was
    nothing here, and there is something over there" needs no schema at all.
    """
    counts = legacy_counts(path)
    # `raw` is excluded from the total on purpose: it is not carried across, so
    # counting it would tell the agent it has 646 nodes to work through when the
    # real list is 128, and a wildly wrong denominator makes the task read as
    # hopeless before it starts. 0.2.0 rebuilds raw from the wiki, which is
    # untouched on disk.
    carried = {kind: n for kind, n in counts.items() if kind != "raw"}
    total = sum(carried.values())
    if not total:
        return {"migrating": False}

    state = {
        "migrating": True,
        "legacy_db": str(Path(path or db.LEGACY_DB_PATH)),
        "legacy_counts": carried,
        "legacy_total": total,
        "legacy_raw_skipped": counts.get("raw", 0),
        "daemon_stopped": _stop_legacy_daemon(),
    }
    _write_state(state)
    return state


def list_nodes(
    limit: int = 25, offset: int = 0, path: Path | None = None
) -> list[dict[str, Any]]:
    """Old nodes for the agent to read and re-write, newest last.

    Excludes superseded nodes and `raw` chunks. Superseded ones are history the
    new store has no way to express without also carrying their successors and
    the pointers between them, which is a lot of machinery for nodes that
    retrieval already filters out. Raw chunks come back in 0.2.0 from the wiki
    itself, which is still on disk.
    """
    connection = _open_legacy(path)
    if connection is None:
        return []
    try:
        rows = connection.execute(
            "SELECT id, title, summary, type, about_user, scope, "
            "       window_start, window_end, stale, locator, source_session "
            "FROM nodes "
            "WHERE superseded_by IS NULL AND type != 'raw' AND origin = 'original' "
            "ORDER BY COALESCE(window_start, updated), id "
            "LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        connection.close()


def status(connection: sqlite3.Connection | None = None) -> dict[str, Any]:
    """Where the migration stands, in terms an agent can act on."""
    state = read_state()
    if not state.get("migrating"):
        return {"migrating": False, "status": "nothing to migrate"}

    written = 0
    if connection is not None:
        written = connection.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]

    remaining = len(list_nodes(limit=10**6, path=Path(state["legacy_db"])))
    return {
        "migrating": True,
        "status": "in progress",
        "legacy_db": state["legacy_db"],
        "legacy_to_carry": remaining,
        "legacy_by_type": state.get("legacy_counts", {}),
        "written_so_far": written,
        "prefs_go_to_files": PREF_TYPES,
        "next": "python -m claude_memory migration list --limit 25 --offset N",
    }


def done() -> dict[str, Any]:
    """Clear the flag. The old store is left exactly where it is.

    Not deleted, and not moved: it cost nothing to keep, it is the only copy of
    anything the agent chose not to carry over, and deleting a user's memory as
    the final step of an upgrade is not a decision this code gets to make.
    """
    state = read_state()
    state["migrating"] = False
    _write_state(state)
    return {
        "migrating": False,
        "status": "migration marked complete",
        "legacy_db_left_at": state.get("legacy_db", str(db.LEGACY_DB_PATH)),
    }
