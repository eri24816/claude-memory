"""Local inspector for the memory store.

    python -m claude_memory.server

A read-only viewer, not a control surface: the agent writes, this shows what it
did and what any of the three read paths would return.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Query
from fastapi.responses import FileResponse

from . import db, retrieval

STATIC_DIR = Path(__file__).with_name("static")

app = FastAPI(title="claude-memory inspector")


def _connection() -> sqlite3.Connection:
    return db.connect()


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


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
) -> list[dict[str, Any]]:
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
        clauses.append("(summary LIKE ? OR title LIKE ? OR id LIKE ?)")
        params.extend([f"%{q}%"] * 3)

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    connection = _connection()
    try:
        rows = connection.execute(
            f"""
            SELECT id, title, summary, type, about_user, scope,
                   window_start, window_end, stale, superseded_by, origin,
                   parent, source_session, updated
            FROM nodes {where}
            ORDER BY updated DESC
            LIMIT ?
            """,
            (*params, limit),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        connection.close()


@app.get("/api/t1")
def t1(scope: str | None = None) -> dict[str, Any]:
    connection = _connection()
    try:
        selected = retrieval.assemble_t1(connection, scope=scope)
        block = retrieval.render_t1(selected)
        return {
            "nodes": selected,
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
    limit: int = Query(default=10, le=50),
) -> list[dict[str, Any]]:
    connection = _connection()
    try:
        return retrieval.search(connection, q, limit=limit, scope=scope)
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
        return {
            "would_retrieve": True,
            "reason": "passed the content-word gate",
            "hits": retrieval.search(connection, message, limit=5, scope=scope),
        }
    finally:
        connection.close()


def main() -> None:
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8765, log_level="warning")


if __name__ == "__main__":
    main()
