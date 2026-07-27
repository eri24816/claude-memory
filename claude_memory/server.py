"""Local inspector for the memory store.

    python -m claude_memory.server

A read-only viewer, not a control surface: the agent writes, this shows what it
did and what any of the three read paths would return.
"""

from __future__ import annotations

import sqlite3
import math
import struct
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Query
from fastapi.responses import FileResponse

from . import db, retrieval

STATIC_DIR = Path(__file__).with_name("static")

app = FastAPI(title="claude-memory inspector")


def _connection() -> sqlite3.Connection:
    return db.connect()


def _project_2d(vectors: list[list[float]], iterations: int = 32) -> list[tuple[float, float]]:
    """Project vectors onto their first two principal components.

    Power iteration is applied through X^T X without materialising the 384x384
    covariance matrix. The fixed initial vectors make the inspector stable
    between reloads.
    """
    if not vectors:
        return []
    dimensions = len(vectors[0])
    mean = [sum(vector[j] for vector in vectors) / len(vectors) for j in range(dimensions)]
    centered = [[value - mean[j] for j, value in enumerate(vector)] for vector in vectors]

    def component(seed: int, previous: list[float] | None = None) -> list[float]:
        direction = [
            math.sin((j + 1) * (seed + 1) * 1.61803398875)
            for j in range(dimensions)
        ]
        for _ in range(iterations):
            scores = [sum(a * b for a, b in zip(row, direction)) for row in centered]
            candidate = [
                sum(row[j] * score for row, score in zip(centered, scores))
                for j in range(dimensions)
            ]
            if previous is not None:
                overlap = sum(a * b for a, b in zip(candidate, previous))
                candidate = [value - overlap * axis for value, axis in zip(candidate, previous)]
            norm = math.sqrt(sum(value * value for value in candidate))
            if norm < 1e-12:
                return [0.0] * dimensions
            direction = [value / norm for value in candidate]
        return direction

    first = component(0)
    second = component(1, first)
    return [
        (
            sum(a * b for a, b in zip(row, first)),
            sum(a * b for a, b in zip(row, second)),
        )
        for row in centered
    ]


@app.get("/")
def index() -> FileResponse:
    """The shell, explicitly uncacheable.

    Everything is inlined in this one file, so with only etag/last-modified the
    browser applies *heuristic* caching and goes on serving an old copy against
    a new server. That is not hypothetical here: after the claim/detail rename
    the page rendered a `SUMMARY` header and blank rows from cache while the API
    was already returning correct data.
    """
    return FileResponse(
        STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"}
    )


@app.get("/api/stats")
def stats() -> dict[str, Any]:
    connection = _connection()
    try:
        by_type = {
            row["type"]: row["count"]
            for row in connection.execute(
                "SELECT type, COUNT(*) AS count FROM nodes GROUP BY type ORDER BY type"
            )
        }
        totals = connection.execute(
            """
            SELECT COUNT(*) AS total,
                   SUM(stale) AS stale,
                   SUM(superseded_by IS NOT NULL) AS superseded
            FROM nodes
            """
        ).fetchone()
        scopes = [
            row["scope"]
            for row in connection.execute(
                "SELECT DISTINCT scope FROM nodes ORDER BY scope"
            )
        ]
        return {
            "by_type": by_type,
            "total": totals["total"] or 0,
            "stale": totals["stale"] or 0,
            "superseded": totals["superseded"] or 0,
            "scopes": scopes,
            "db": str(db.DEFAULT_DB_PATH),
        }
    finally:
        connection.close()


@app.get("/api/nodes")
def nodes(
    type: str | None = None,
    scope: str | None = None,
    about_user: str | None = None,
    state: str = "all",
    q: str | None = None,
    limit: int = 200,
    offset: int = 0,
) -> dict[str, Any]:
    """One page of nodes, with the total the page was cut from.

    The store passed 600 nodes while this returned a bare list capped at 200,
    so a node written outside the newest 200 was simply absent from the tab with
    nothing on screen saying so -- it read as "not in memory" rather than "not on
    this page". `total` is what the page count is drawn from, and is the count
    after filtering, not of the table.
    """
    clauses: list[str] = []
    params: list[Any] = []

    if type:
        clauses.append("type = ?")
        params.append(type)
    if scope:
        clauses.append("scope = ?")
        params.append(scope)
    if about_user in {"true", "false"}:
        clauses.append("about_user = ?")
        params.append(1 if about_user == "true" else 0)
    if state == "current":
        clauses.append("superseded_by IS NULL AND stale = 0")
    elif state == "stale":
        clauses.append("stale = 1")
    elif state == "superseded":
        clauses.append("superseded_by IS NOT NULL")
    if q:
        clauses.append("(claim LIKE ? OR detail LIKE ? OR id LIKE ?)")
        params.extend([f"%{q}%"] * 3)

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    connection = _connection()
    try:
        total = connection.execute(
            f"SELECT COUNT(*) AS total FROM nodes {where}", params
        ).fetchone()["total"]
        rows = connection.execute(
            f"""
            SELECT id, claim, detail, type, about_user, scope,
                   window_start, window_end, stale, superseded_by, origin,
                   parent, source_session, updated
            FROM nodes {where}
            ORDER BY updated DESC, id DESC
            LIMIT ? OFFSET ?
            """,
            (*params, limit, offset),
        ).fetchall()
        nodes = []
        for row in rows:
            node = dict(row)
            # "superseded" alone tells you a row is dead without telling you
            # what is alive, which is the only reason to care that it died.
            node["correction"] = retrieval.newest_correction(
                connection, node["superseded_by"]
            )
            nodes.append(node)
        # `id` breaks ties on `updated`, which a bulk ingest gives hundreds of
        # nodes to the second: without it SQLite may order a tie differently
        # between two queries, and a row can be skipped by one page and repeated
        # by the next.
        return {"nodes": nodes, "total": total, "offset": offset, "limit": limit}
    finally:
        connection.close()


@app.get("/api/graph")
def graph(scope: str | None = None) -> dict[str, Any]:
    """Return a stable PCA layout of the stored semantic embeddings."""
    connection = _connection()
    try:
        clauses = ["n.superseded_by IS NULL"]
        params: list[Any] = []
        if scope:
            clauses.append("n.scope IN ('global', ?)")
            params.append(scope)
        rows = connection.execute(
            f"""
            SELECT n.id, n.claim, n.detail, n.type, n.about_user, n.scope,
                   n.parent, n.stale, v.embedding
            FROM nodes AS n
            JOIN nodes_vec AS v ON v.rowid = n.rowid
            WHERE {' AND '.join(clauses)}
            ORDER BY n.id
            """,
            params,
        ).fetchall()
        vectors = [
            list(struct.unpack(f"<{len(row['embedding']) // 4}f", row["embedding"]))
            for row in rows
        ]
        coordinates = _project_2d(vectors)
        points = []
        for row, (x, y) in zip(rows, coordinates):
            point = dict(row)
            point.pop("embedding")
            point.update(x=x, y=y)
            points.append(point)
        return {"algorithm": "PCA", "dimensions": len(vectors[0]) if vectors else 0, "points": points}
    finally:
        connection.close()


@app.get("/api/t1")
def t1(scope: str | None = None) -> dict[str, Any]:
    connection = _connection()
    try:
        selected = retrieval.assemble_t1(connection, scope=scope)
        history = retrieval.session_history(connection, scope=scope)
        block = retrieval.render_t1(selected, history)
        return {
            "nodes": selected,
            "history": history,
            "block": block,
            "characters": len(block),
            "budget": 8000,
        }
    finally:
        connection.close()


@app.get("/api/search")
def search(
    q: str,
    scope: str | None = None,
    type: str | None = None,
    limit: int = Query(default=10, le=50),
) -> dict[str, Any]:
    connection = _connection()
    try:
        hits = retrieval.search(
            connection, q, limit=limit, scope=scope, node_type=type,
        )
        return {
            "hits": hits,
            "block": retrieval.render_context(
                hits, heading=f"# Memory — search: {q}"
            ),
        }
    finally:
        connection.close()


@app.get("/api/dig")
def dig(handle: str) -> dict[str, Any]:
    """Expand one node by the id or claim retrieval printed.

    A fragment matching several nodes comes back as an error rather than as a
    miss -- reporting "nothing found" for an ambiguous handle is how a caller
    ends up writing a duplicate of something already stored.
    """
    connection = _connection()
    try:
        node = retrieval.dig(connection, handle)
        ambiguous = bool(node and node.get("error"))
        return {
            "found": node is not None and not ambiguous,
            "ambiguous": ambiguous,
            "node": None if ambiguous else node,
            "block": retrieval.render_digs([(handle, node)]),
        }
    finally:
        connection.close()


@app.get("/api/auto")
def auto(message: str, scope: str | None = None) -> dict[str, Any]:
    """Simulate the per-message retrieval path before the hook is wired."""
    would_retrieve = retrieval.should_retrieve(message)
    if not would_retrieve:
        return {"would_retrieve": False, "reason": "too few content words", "hits": []}

    connection = _connection()
    try:
        hits = retrieval.search_stratified(connection, message, scope=scope)
        return {
            "would_retrieve": True,
            "reason": "passed the content-word gate",
            "hits": hits,
            "block": retrieval.render_context(hits),
        }
    finally:
        connection.close()


def main() -> None:
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8765, log_level="warning")


if __name__ == "__main__":
    main()
