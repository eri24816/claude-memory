"""Reads: hybrid BM25 + vector retrieval, and the categorical T1 assembly."""

from __future__ import annotations

import re
import sqlite3
from typing import Any

from . import embed

RRF_K = 60
CANDIDATE_K = 200
FINAL_K = 10

LEAD_TIME_DAYS = 30
RECENT_N = 3

_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_]+")


def _fts_query(text: str) -> str:
    """Quote each token so punctuation cannot break FTS5 MATCH syntax."""
    tokens = _TOKEN_PATTERN.findall(text)
    return " OR ".join(f'"{token}"' for token in tokens)


def search(
    connection: sqlite3.Connection,
    query: str,
    limit: int = FINAL_K,
    scope: str | None = None,
    include_superseded: bool = False,
    parent: str | None = None,
) -> list[dict[str, Any]]:
    """Hybrid retrieval fused by Reciprocal Rank Fusion.

    RRF fuses by rank rather than score because BM25 and cosine distance are on
    incomparable scales; blending them by weight needs per-corpus calibration.
    """
    match_expression = _fts_query(query)
    if not match_expression:
        return []

    scopes = ["global"] + ([scope] if scope and scope != "global" else [])
    scope_placeholders = ", ".join("?" * len(scopes))

    parent_clause = "n.parent IS NULL" if parent is None else "n.parent = ?"
    parent_params: tuple[Any, ...] = () if parent is None else (parent,)

    superseded_clause = "" if include_superseded else "AND n.superseded_by IS NULL"

    query_vector = embed.serialize(embed.encode_one(query))

    sql = f"""
        WITH lexical AS (
            SELECT rowid AS node_rowid,
                   ROW_NUMBER() OVER (ORDER BY bm25(nodes_fts)) AS rank
            FROM nodes_fts
            WHERE nodes_fts MATCH ?
            LIMIT ?
        ),
        semantic AS (
            SELECT rowid AS node_rowid,
                   ROW_NUMBER() OVER (ORDER BY distance) AS rank
            FROM nodes_vec
            WHERE embedding MATCH ? AND k = ?
        )
        SELECT n.id, n.title, n.summary, n.type, n.scope, n.about_user,
               n.window_start, n.window_end, n.stale,
               COALESCE(1.0 / (? + lexical.rank), 0)
             + COALESCE(1.0 / (? + semantic.rank), 0) AS score
        FROM nodes n
        LEFT JOIN lexical  ON lexical.node_rowid  = n.rowid
        LEFT JOIN semantic ON semantic.node_rowid = n.rowid
        WHERE (lexical.rank IS NOT NULL OR semantic.rank IS NOT NULL)
          AND n.scope IN ({scope_placeholders})
          AND {parent_clause}
          {superseded_clause}
        ORDER BY score DESC
        LIMIT ?
    """

    rows = connection.execute(
        sql,
        (match_expression, CANDIDATE_K, query_vector, CANDIDATE_K, RRF_K, RRF_K,
         *scopes, *parent_params, limit),
    ).fetchall()
    return [dict(row) for row in rows]


def assemble_t1(
    connection: sqlite3.Connection,
    scope: str | None = None,
    lead_time_days: int = LEAD_TIME_DAYS,
    recent_n: int = RECENT_N,
) -> list[dict[str, Any]]:
    """The autoload set. Categorical, not ranked: whole sets with caps on the
    volatile types, so no priority function is needed."""
    scopes = ["global"] + ([scope] if scope and scope != "global" else [])
    scope_placeholders = ", ".join("?" * len(scopes))
    lead = f"+{lead_time_days} day"

    always_and_current = connection.execute(
        f"""
        SELECT id, title, summary, type, scope, window_start, window_end
        FROM nodes
        WHERE superseded_by IS NULL
          AND scope IN ({scope_placeholders})
          AND (
                type IN ('meta', 'conv-pref')
             OR (about_user = 1 AND type = 'fact'
                 AND stale = 0
                 AND (window_end IS NULL OR date('now') <= window_end)
                 AND (window_start IS NULL
                      OR window_start <= date('now', ?)))
             OR (about_user = 1 AND type = 'todo'
                 AND stale = 0)
          )
        ORDER BY type, window_start
        """,
        (*scopes, lead),
    ).fetchall()

    recent: list[sqlite3.Row] = []
    for node_type in ("idea", "action", "intention"):
        recent.extend(
            connection.execute(
                f"""
                SELECT id, title, summary, type, scope, window_start, window_end
                FROM nodes
                WHERE superseded_by IS NULL
                  AND stale = 0
                  AND about_user = 1
                  AND type = ?
                  AND scope IN ({scope_placeholders})
                ORDER BY COALESCE(window_start, updated) DESC
                LIMIT ?
                """,
                (node_type, *scopes, recent_n),
            ).fetchall()
        )

    return [dict(row) for row in [*always_and_current, *recent]]


def render_t1(nodes: list[dict[str, Any]]) -> str:
    """Render the autoload block that gets injected at session start."""
    if not nodes:
        return ""
    by_type: dict[str, list[dict[str, Any]]] = {}
    for node in nodes:
        by_type.setdefault(node["type"], []).append(node)

    lines = ["# Memory"]
    for node_type in sorted(by_type):
        lines.append(f"\n## {node_type}")
        for node in by_type[node_type]:
            window = ""
            if node.get("window_start") or node.get("window_end"):
                window = f" [{node.get('window_start') or ''}..{node.get('window_end') or ''}]"
            lines.append(f"- {node['summary']}{window}")
    return "\n".join(lines)
