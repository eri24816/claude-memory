"""v0 -> v1 migration.

Split by what a program can decide. The schema change, the relocation, the
deletion of retired types and the extraction of prefs into files are all
mechanical and run in one shot. Rewriting 684-character summaries into 8-word
claims is a judgement call per node, so an agent does it — but as a *cursor*,
not a batch: the state of the migration is exactly "which nodes still have
claim IS NULL", so it is idempotent, resumable across sessions, and cannot be
left half-applied by an agent that runs out of context in the middle.

The user-facing instruction is one line — "migrate to v0.1.0" in Claude Code —
and MIGRATION-0.1.0.md is what the agent reads to carry it out.
"""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path
from typing import Any

from . import db, embed, settings
from .models import CLAIM_MAX_WORDS, InvariantError

BACKUP_SUFFIX = ".pre-0.1.0"

# v0 stored these as nodes; v1 stores them as files. The mapping is the whole
# extraction step -- everything else about them is discarded, because a rule has
# no window, no about_user and no supersession history worth keeping.
PREF_TYPES = {"conv-pref": "conv", "code-pref": "code", "meta": "meta"}


def _stop_daemon() -> int | None:
    """Stop the embedding daemon, tolerating every way that can fail.

    Best-effort by design: a daemon that is not running, cannot be reached, or
    refuses to die must not block a migration. The cost of it surviving is a
    stale reader, not a corrupt store.
    """
    try:
        from . import daemon

        return daemon.stop()
    except Exception:  # pragma: no cover - defensive
        return None


def backup_path(target: Path) -> Path:
    return target.with_suffix(BACKUP_SUFFIX + target.suffix)


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}


def relocate(target: Path | None = None) -> dict[str, Any]:
    """Bring a v0 store at the old ~/.claude path over to settings/.

    Copies rather than moves, and refuses to overwrite anything real. A store is
    the least replaceable thing this project owns; leaving the original where it
    was means a failed migration costs nothing but disk.
    """
    target = Path(target or db.DEFAULT_DB_PATH)
    legacy = db.LEGACY_DB_PATH

    if not legacy.exists() or legacy.resolve() == target.resolve():
        return {"relocated": False, "reason": "no legacy store"}

    if target.exists() and target.stat().st_size > 0:
        with sqlite3.connect(str(target)) as probe:
            populated = probe.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='nodes'"
            ).fetchone() is not None
            if populated and probe.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]:
                return {"relocated": False, "reason": "target already has nodes"}

    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(legacy, target)
    return {"relocated": True, "from": str(legacy), "to": str(target)}


def _extract_prefs(connection: sqlite3.Connection) -> dict[str, Any]:
    """Dump each retired type into its settings file, verbatim, then delete.

    Verbatim on purpose: this step must not lose anything, and it is not the
    step that improves the text. Consolidation -- merging the restatements that
    accumulated because nodes could only be appended -- is the agent's job, and
    it is spelled out in the file itself so whoever opens it next knows the
    dump is a starting point rather than the finished document.
    """
    written: dict[str, int] = {}

    # meta is the exception: it describes the memory system, and the system is
    # what just changed. Every v0 meta node talks about titles, summaries, raw
    # chunks and refinement-on-demand -- none of which exist in v1 -- so dumping
    # them verbatim would preload actively wrong instructions into every
    # session. It is shipped content, not user content, so it is replaced.
    settings.write("meta", (Path(__file__).with_name("meta_template.md"))
                   .read_text(encoding="utf-8"))
    written["meta"] = "replaced with the v1 template"

    for node_type, name in PREF_TYPES.items():
        if name == "meta":
            continue
        rows = connection.execute(
            "SELECT title, summary FROM nodes "
            "WHERE type = ? AND superseded_by IS NULL ORDER BY updated",
            (node_type,),
        ).fetchall()
        if not rows:
            continue

        lines = [
            f"# {settings.FILES[name]}",
            "",
            "<!-- Migrated verbatim from v0 nodes. These were append-only, so "
            "several of them restate each other; consolidate them into coherent "
            "sections. Rewriting this file in place is the point. -->",
            "",
        ]
        for row in rows:
            body = " ".join((row["summary"] or "").split())
            lines.append(f"- {body}")
        settings.write(name, "\n".join(lines))
        written[name] = len(rows)

    placeholders = ", ".join("?" * len(PREF_TYPES))
    connection.execute(
        f"DELETE FROM nodes WHERE type IN ({placeholders})", tuple(PREF_TYPES)
    )
    return written


def _transform(connection: sqlite3.Connection) -> dict[str, Any]:
    """The schema change. Leaves `title` in place and `claim` NULL on purpose.

    The agent needs the old title AND the old summary to write a good claim, so
    title survives until every node has one; `_finalize` drops it. Until then
    `claim IS NULL` is the migration's entire state.
    """
    existing = _columns(connection, "nodes")
    report: dict[str, Any] = {}

    report["prefs"] = _extract_prefs(connection)

    # Raw nodes and their vectors go entirely. Nothing is lost that re-ingest
    # cannot rebuild, and 0.2.0 rebuilds it with a per-query cap instead of the
    # demotion that never earned its keep.
    report["raw_dropped"] = connection.execute(
        "SELECT COUNT(*) FROM nodes WHERE origin = 'derived'"
    ).fetchone()[0]
    connection.execute("DELETE FROM nodes WHERE origin = 'derived'")

    # Edges and supersession pointers into deleted nodes would dangle.
    connection.execute(
        "DELETE FROM node_edges WHERE src_id NOT IN (SELECT id FROM nodes) "
        "OR dst_id NOT IN (SELECT id FROM nodes)"
    )
    connection.execute(
        "UPDATE nodes SET superseded_by = NULL WHERE superseded_by NOT IN "
        "(SELECT id FROM nodes)"
    )
    connection.execute(
        "UPDATE nodes SET parent = NULL WHERE parent NOT IN (SELECT id FROM nodes)"
    )

    connection.execute("DROP INDEX IF EXISTS nodes_title")
    if "summary" in existing:
        connection.execute("ALTER TABLE nodes RENAME COLUMN summary TO detail")
    if "claim" not in existing:
        connection.execute("ALTER TABLE nodes ADD COLUMN claim TEXT")

    revision_columns = _columns(connection, "node_revisions")
    if "summary" in revision_columns:
        connection.execute("ALTER TABLE node_revisions RENAME COLUMN summary TO detail")
    if "claim" not in revision_columns:
        connection.execute("ALTER TABLE node_revisions ADD COLUMN claim TEXT")

    # Both indexes are rebuilt from scratch in _finalize: the FTS columns changed
    # shape, and every vector was computed from a title and a summary that will
    # not exist by then. An empty index mid-migration is honest -- search is
    # genuinely unreliable until the claims are written.
    connection.execute("DROP TABLE IF EXISTS nodes_fts")
    connection.execute("DELETE FROM nodes_vec")
    connection.executescript(db.SCHEMA_PATH.read_text(encoding="utf-8"))

    connection.commit()
    report["pending"] = _pending_count(connection)
    return report


def _pending_count(connection: sqlite3.Connection) -> int:
    return connection.execute(
        "SELECT COUNT(*) FROM nodes WHERE claim IS NULL OR claim = ''"
    ).fetchone()[0]


def run(
    connection: sqlite3.Connection | None = None,
    target: Path | str | None = None,
) -> dict[str, Any]:
    """Deterministic phase: relocate, back up, transform. Safe to re-run.

    `target` is where the store lives on disk, which the connection alone cannot
    tell us -- the backup and the relocation are file operations. Defaults to the
    configured path so the common case needs no argument.
    """
    target = Path(target or db.DEFAULT_DB_PATH)

    # Stop the daemon first, and do it here rather than telling the user to.
    # It is the only long-lived process in the system: every session touches the
    # store through short-lived hook processes that pick up new code for free,
    # while the daemon holds whatever it imported at start-up. Left running, it
    # would go on serving retrieval from the pre-migration build against a store
    # that no longer matches it. It also opens a connection on any session's
    # message, which is precisely the concurrent writer the DDL below cannot
    # tolerate. SessionStart brings it back automatically afterwards.
    stopped = _stop_daemon()

    moved = relocate(target)
    connection = connection or db.connect(target)
    if not db.needs_migration(connection):
        return {"status": "already at v1", "version": db.user_version(connection)}

    if "claim" not in _columns(connection, "nodes"):
        backup = backup_path(target)
        if not backup.exists():
            connection.commit()
            connection.execute("VACUUM INTO ?", (str(backup),))
        report = _transform(connection)
        report["backup"] = str(backup)
    else:
        report = {"pending": _pending_count(connection)}

    report["relocation"] = moved
    report["daemon_stopped"] = stopped
    report["status"] = "schema converted; claims pending"
    report["next"] = (
        "python -m claude_memory migrate --next 20  # then --set with the claims"
    )
    if report["pending"] == 0:
        report.update(_finalize(connection))
    return report


def status(connection: sqlite3.Connection) -> dict[str, Any]:
    if not db.needs_migration(connection):
        return {"version": db.user_version(connection), "status": "up to date"}
    if "claim" not in _columns(connection, "nodes"):
        return {"version": 0, "status": "not started",
                "next": "python -m claude_memory migrate"}
    return {"version": 0, "status": "claims pending",
            "pending": _pending_count(connection),
            "done": connection.execute(
                "SELECT COUNT(*) FROM nodes WHERE claim IS NOT NULL AND claim != ''"
            ).fetchone()[0]}


def next_batch(connection: sqlite3.Connection, limit: int = 20) -> list[dict[str, Any]]:
    """The next N nodes still awaiting a claim, with everything needed to write one."""
    rows = connection.execute(
        "SELECT id, title, detail, type, about_user, window_start, window_end "
        "FROM nodes WHERE claim IS NULL OR claim = '' ORDER BY updated LIMIT ?",
        (limit,),
    ).fetchall()
    return [dict(row) for row in rows]


def set_claims(
    connection: sqlite3.Connection, specs: list[dict[str, Any]]
) -> dict[str, Any]:
    """Write claims for a batch, then finalize automatically once none remain.

    Validates the word cap here rather than trusting the caller: this is the one
    path by which every pre-existing node acquires its claim, and a migration
    that quietly admits 20-word claims would leave the store violating the
    invariant that the whole redesign rests on.
    """
    updated: list[str] = []
    for spec in specs:
        node_id, claim = spec.get("id"), (spec.get("claim") or "").strip()
        if not node_id or not claim:
            raise InvariantError(f"each entry needs an id and a claim: {spec!r}")
        claim = " ".join(claim.split())
        if len(claim.split()) > CLAIM_MAX_WORDS:
            raise InvariantError(
                f"claim for {node_id!r} is {len(claim.split())} words, "
                f"max {CLAIM_MAX_WORDS}: {claim!r}"
            )
        if connection.execute("SELECT 1 FROM nodes WHERE id = ?", (node_id,)).fetchone() is None:
            raise InvariantError(f"no node with id {node_id!r}")

        if "detail" in spec:
            detail = spec["detail"]
            detail = detail.strip() if isinstance(detail, str) else detail
            connection.execute(
                "UPDATE nodes SET claim = ?, detail = ? WHERE id = ?",
                (claim, detail or None, node_id),
            )
        else:
            connection.execute(
                "UPDATE nodes SET claim = ? WHERE id = ?", (claim, node_id)
            )
        updated.append(node_id)

    connection.commit()
    result: dict[str, Any] = {"updated": updated, "pending": _pending_count(connection)}
    if result["pending"] == 0:
        result.update(_finalize(connection))
    return result


def _finalize(connection: sqlite3.Connection) -> dict[str, Any]:
    """Drop `title`, rebuild both indexes, stamp the version.

    Only reachable with zero pending claims, so the NOT NULL that v1's schema
    declares on `claim` is true of every row by the time anything relies on it.
    """
    if "title" in _columns(connection, "nodes"):
        connection.execute("ALTER TABLE nodes DROP COLUMN title")

    connection.execute("DELETE FROM nodes_fts")
    connection.execute("DELETE FROM nodes_vec")

    rows = connection.execute(
        "SELECT rowid, claim, detail, type, scope FROM nodes"
    ).fetchall()
    for row in rows:
        text = f"{row['claim']}. {row['detail']}" if row["detail"] else row["claim"]
        connection.execute(
            "INSERT INTO nodes_fts (rowid, claim, detail, keywords) VALUES (?, ?, ?, ?)",
            (row["rowid"], row["claim"], row["detail"] or "",
             f"{row['type']} {row['scope']}"),
        )
        connection.execute(
            "INSERT INTO nodes_vec (rowid, embedding) VALUES (?, ?)",
            (row["rowid"], embed.serialize(embed.encode_one(text))),
        )

    connection.execute(f"PRAGMA user_version = {db.SCHEMA_VERSION}")
    connection.commit()
    return {"status": "migrated", "version": db.SCHEMA_VERSION, "reindexed": len(rows)}


def rollback(target: Path | None = None) -> dict[str, Any]:
    """Restore the pre-migration snapshot taken by run()."""
    target = Path(target or db.DEFAULT_DB_PATH)
    backup = backup_path(target)
    if not backup.exists():
        return {"restored": False, "reason": f"no backup at {backup}"}
    shutil.copy2(backup, target)
    return {"restored": True, "from": str(backup), "to": str(target)}
