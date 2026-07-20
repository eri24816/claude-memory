"""Writes. The only supported mutations are insert, supersede, and stale.

Summaries are immutable: there is deliberately no code path that updates one.
Reality changing is recorded by supersession; a bad capture is undone through
node_revisions.
"""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable

from . import embed
from .models import (
    TYPES_NEVER_SUPERSEDED,
    InvariantError,
    Node,
    now,
    slugify,
    validate,
)

NODE_COLUMNS = (
    "id", "title", "summary", "type", "about_user", "scope",
    "window_start", "window_end", "stale", "superseded_by", "origin",
    "parent", "derived_from", "content_hash", "locator", "source_session",
    "updated",
)


def new_run_id() -> str:
    return uuid.uuid4().hex[:12]


def _unique_id(connection: sqlite3.Connection, candidate: str) -> str:
    identifier = candidate
    suffix = 2
    while connection.execute(
        "SELECT 1 FROM nodes WHERE id = ?", (identifier,)
    ).fetchone():
        identifier = f"{candidate}-{suffix}"
        suffix += 1
    return identifier


def _unique_title(connection: sqlite3.Connection, candidate: str) -> str:
    """Suffix a colliding title until it is free."""
    title = candidate
    suffix = 2
    while connection.execute(
        "SELECT 1 FROM nodes WHERE title = ?", (title,)
    ).fetchone():
        title = f"{candidate} ({suffix})"
        suffix += 1
    return title


def _retire_title(connection: sqlite3.Connection, node_id: str, title: str) -> None:
    """Free a title held by a node being superseded, suffixing the old holder.

    A title names a concept, not a version, so the current node has to be the one
    it resolves to. The alternative — letting the newcomer take the suffix —
    means every re-statement pushes the live node further from its natural name
    while the bare title keeps pointing at something dead.

    Superseded nodes are excluded from retrieval anyway, so the suffixed title is
    only ever reached by deliberately digging through history.
    """
    connection.execute(
        "UPDATE nodes SET title = ? WHERE id = ?",
        (_unique_title(connection, title), node_id),
    )


def resolve(connection: sqlite3.Connection, handle: str) -> str:
    """Resolve a title or an id to an id.

    Everything an agent is shown carries a title and never an id, so any handle
    it hands back is a title. Ids stay accepted because the ingest and the tests
    hold them directly, but a title has to work or supersede and stale are
    unreachable from the only surface that uses them.
    """
    row = connection.execute(
        "SELECT id FROM nodes WHERE title = ?", (handle,)
    ).fetchone()
    if row is not None:
        return row["id"]
    if connection.execute(
        "SELECT 1 FROM nodes WHERE id = ?", (handle,)
    ).fetchone():
        return handle
    raise InvariantError(f"no node titled {handle!r}")


def _next_revision(connection: sqlite3.Connection, node_id: str) -> int:
    row = connection.execute(
        "SELECT COALESCE(MAX(revision), 0) + 1 FROM node_revisions WHERE node_id = ?",
        (node_id,),
    ).fetchone()
    return row[0]


def _record_revision(
    connection: sqlite3.Connection,
    node_id: str,
    op: str,
    summary: str | None,
    capture_run_id: str | None,
) -> None:
    connection.execute(
        """
        INSERT INTO node_revisions (node_id, revision, op, summary,
                                    capture_run_id, recorded_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (node_id, _next_revision(connection, node_id), op, summary,
         capture_run_id, now()),
    )


def _index(connection: sqlite3.Connection, rowid: int, node: Node) -> None:
    connection.execute(
        "INSERT INTO nodes_fts (rowid, title, summary, keywords) VALUES (?, ?, ?, ?)",
        (rowid, node.title or "", node.summary, node.lexical_keywords()),
    )
    vector = embed.encode_one(node.embedding_text())
    connection.execute(
        "INSERT INTO nodes_vec (rowid, embedding) VALUES (?, ?)",
        (rowid, embed.serialize(vector)),
    )


def _insert(
    connection: sqlite3.Connection, node: Node, capture_run_id: str | None
) -> str:
    validate(node)
    node.title = _unique_title(connection, node.title)
    node.updated = node.updated or now()
    cursor = connection.execute(
        f"INSERT INTO nodes ({', '.join(NODE_COLUMNS)}) "
        f"VALUES ({', '.join('?' * len(NODE_COLUMNS))})",
        (
            node.id, node.title, node.summary, node.type,
            None if node.about_user is None else int(node.about_user),
            node.scope, node.window_start, node.window_end, int(node.stale),
            node.superseded_by, node.origin, node.parent, node.derived_from,
            node.content_hash, node.locator, node.source_session, node.updated,
        ),
    )
    _index(connection, cursor.lastrowid, node)
    _record_revision(connection, node.id, "insert", node.summary, capture_run_id)
    return node.id


def _supersede(
    connection: sqlite3.Connection,
    old_id: str,
    new_node: Node,
    capture_run_id: str | None,
) -> str:
    target = connection.execute(
        "SELECT id, type, title, superseded_by FROM nodes WHERE id = ?", (old_id,)
    ).fetchone()

    if target is None:
        raise InvariantError(f"cannot supersede {old_id!r}: no such node")
    if target["type"] in TYPES_NEVER_SUPERSEDED:
        raise InvariantError(
            f"cannot supersede {old_id!r}: a {target['type']} stays valid however "
            "much work it motivates; link it with a 'motivates' edge instead"
        )
    if target["superseded_by"] is not None:
        raise InvariantError(
            f"cannot supersede {old_id!r}: already superseded by "
            f"{target['superseded_by']!r}"
        )

    if new_node.title == target["title"]:
        _retire_title(connection, target["id"], target["title"])

    new_id = _insert(connection, new_node, capture_run_id)
    connection.execute(
        "UPDATE nodes SET superseded_by = ?, updated = ? WHERE id = ?",
        (new_id, now(), old_id),
    )
    _record_revision(connection, old_id, "supersede", None, capture_run_id)
    return new_id


def _is_authored_message(message: dict) -> bool:
    """True for something Eric actually typed, not a tool_result echoed back as
    a user-role turn -- the Anthropic API's convention for feeding tool output
    back into the conversation. A transcript's most recent role=user event is
    very often the harness's own tool-result turn, not Eric's last message; a
    locator pointing at a raw tool_result blob is far less useful than one
    pointing at what he actually asked.
    """
    content = message.get("content")
    if isinstance(content, str):
        return bool(content.strip())
    if isinstance(content, list):
        return any(
            isinstance(block, dict) and block.get("type") == "text"
            for block in content
        )
    return False


@lru_cache(maxsize=1)
def _live_transcript_uuid() -> str | None:
    """Best-effort: the most recent Eric-authored message's uuid in the live
    transcript.

    locator's default for a node whose source_session is this live session --
    an agent capturing something during the conversation that produced it gets
    a jump-to-context anchor with zero extra effort, the same way
    source_session itself defaults. Anchors to the most recent message Eric
    actually typed, since that is usually what a capture is written in
    response to; content established across several turns only gets an
    approximate anchor, which still beats no anchor in a transcript that can
    run to thousands of lines.

    Cached for the process: remember() may write several nodes in one batch,
    and this reads a whole transcript file that only grows across a session --
    re-reading it per node would be real, avoidable cost. A fresh process (the
    normal case: every CLI invocation) gets a fresh read, so nothing goes stale.

    Best-effort by design, like source_session's default: any failure -- no
    session id, transcript not found, unreadable -- returns None rather than
    raising. A missing locator is a minor loss, not a reason to fail the write.
    """
    session_id = os.environ.get("CLAUDE_CODE_SESSION_ID")
    if not session_id:
        return None
    projects_dir = Path(os.environ.get(
        "CLAUDE_MEMORY_TRANSCRIPTS_DIR", str(Path.home() / ".claude" / "projects")
    ))
    try:
        matches = list(projects_dir.glob(f"**/{session_id}.jsonl"))
        if not matches:
            return None
        lines = matches[0].read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None

    for line in reversed(lines):
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        message = event.get("message")
        if (
            isinstance(message, dict) and message.get("role") == "user"
            and event.get("uuid") and _is_authored_message(message)
        ):
            return event["uuid"]
    return None


def _node_from_spec(connection: sqlite3.Connection, spec: dict[str, Any]) -> Node:
    fields = {
        key: value
        for key, value in spec.items()
        if key not in {"op", "supersedes", "error", "id"}
    }
    if fields.get("title"):
        fields["title"] = fields["title"].strip()
    is_original = fields.get("origin", "original") == "original"
    live_session = os.environ.get("CLAUDE_CODE_SESSION_ID")
    if is_original and fields.get("source_session") is None:
        # Only for origin='original': a derived (ingested) node's provenance is
        # the file it came from (derived_from/locator), not whichever session
        # happened to run the ingest command -- stamping a session id there
        # would be actively wrong, not just redundant.
        #
        # A default, not a hardcoded requirement: a backfill or an explicit
        # spec describing a different session's content passes its own value,
        # which wins. Without this, nothing ever set the field -- every node
        # in the store had a NULL source_session until this fix, because the
        # agent has no reason to type its own session id by hand on every
        # write, and mostly didn't know it could.
        fields["source_session"] = live_session

    if (
        is_original and fields.get("locator") is None and live_session
        and fields.get("source_session") == live_session
    ):
        # Only when source_session resolved to THIS live session -- explicit or
        # defaulted, doesn't matter which. A backfill that names some other
        # session's content but omits locator must not get "wherever this
        # process happens to be right now" stamped on it; that would point at
        # the wrong transcript entirely, worse than no locator at all.
        live_uuid = _live_transcript_uuid()
        if live_uuid:
            fields["locator"] = live_uuid

    identifier = spec.get("id") or slugify(fields.get("title") or spec["summary"])
    return Node(id=_unique_id(connection, identifier), **fields)


def remember(
    connection: sqlite3.Connection,
    specs: Iterable[dict[str, Any]],
    capture_run_id: str | None = None,
) -> dict[str, Any]:
    """Apply a batch of insert/supersede operations atomically."""
    capture_run_id = capture_run_id or new_run_id()
    specs = list(specs)
    written: list[str] = []
    pending_edges: list[tuple[str, dict[str, str]]] = []

    try:
        for spec in specs:
            node = _node_from_spec(connection, spec)
            operation = spec.get("op", "insert")

            if operation == "supersede":
                target = spec.get("supersedes")
                if not target:
                    raise InvariantError("op 'supersede' requires 'supersedes'")
                node_id = _supersede(
                    connection, resolve(connection, target), node, capture_run_id
                )
            elif operation == "insert":
                node_id = _insert(connection, node, capture_run_id)
            else:
                raise InvariantError(f"unknown op {operation!r}")

            written.append(node_id)
            pending_edges.extend((node_id, edge) for edge in node.edges)

        for source_id, edge in pending_edges:
            try:
                destination = resolve(connection, edge["dst"])
            except InvariantError:
                raise InvariantError(
                    f"edge {source_id} -{edge['rel']}-> {edge['dst']}: no such node"
                ) from None
            connection.execute(
                "INSERT OR IGNORE INTO node_edges (src_id, dst_id, rel) "
                "VALUES (?, ?, ?)",
                (source_id, destination, edge["rel"]),
            )
    except Exception:
        connection.rollback()
        raise

    connection.commit()
    return {"capture_run_id": capture_run_id, "written": written}


def set_stale(
    connection: sqlite3.Connection, handle: str, capture_run_id: str | None = None
) -> None:
    """Mark a node as no longer true, with the end date unknown."""
    node_id = resolve(connection, handle)
    connection.execute(
        "UPDATE nodes SET stale = 1, updated = ? WHERE id = ?", (now(), node_id)
    )
    _record_revision(connection, node_id, "stale", None, capture_run_id)
    connection.commit()


def rollback_run(connection: sqlite3.Connection, capture_run_id: str) -> dict[str, Any]:
    """Undo a capture run: delete what it inserted, un-supersede what it closed."""
    revisions = connection.execute(
        "SELECT node_id, op FROM node_revisions WHERE capture_run_id = ? "
        "ORDER BY recorded_at DESC",
        (capture_run_id,),
    ).fetchall()

    deleted: list[str] = []
    restored: list[str] = []

    for revision in revisions:
        node_id, op = revision["node_id"], revision["op"]
        if op == "insert":
            row = connection.execute(
                "SELECT rowid FROM nodes WHERE id = ?", (node_id,)
            ).fetchone()
            if row is not None:
                connection.execute("DELETE FROM nodes_fts WHERE rowid = ?", (row["rowid"],))
                connection.execute("DELETE FROM nodes_vec WHERE rowid = ?", (row["rowid"],))
                connection.execute("DELETE FROM node_edges WHERE src_id = ? OR dst_id = ?",
                                   (node_id, node_id))
                connection.execute("DELETE FROM nodes WHERE id = ?", (node_id,))
                deleted.append(node_id)
        elif op in {"supersede", "stale"}:
            column = "superseded_by = NULL" if op == "supersede" else "stale = 0"
            connection.execute(f"UPDATE nodes SET {column} WHERE id = ?", (node_id,))
            restored.append(node_id)

    connection.execute(
        "DELETE FROM node_revisions WHERE capture_run_id = ?", (capture_run_id,)
    )
    connection.commit()
    return {"deleted": deleted, "restored": restored}
