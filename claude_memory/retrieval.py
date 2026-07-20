"""Reads: hybrid BM25 + vector retrieval, and the categorical T1 assembly."""

from __future__ import annotations

import re
import sqlite3
from typing import Any

from . import embed

RRF_K = 60
CANDIDATE_K = 200
FINAL_K = 10

# Raw chunks are unread source text competing against claims someone actually
# judged. They should still surface — that is the whole point of indexing them —
# but a refined node on the same topic should win. Multiplicative, so a raw hit
# that dominates on both halves can still outrank a weak refined one.
RAW_DEMOTION = 0.6

# Sections of one document are near-identical in embedding space, so a matching
# page sweeps the top slots and buries every other source. Capping per source
# document costs a little depth on the best match and buys breadth across the
# corpus, which is what a memory lookup is for.
MAX_PER_SOURCE = 2

LEAD_TIME_DAYS = 30
RECENT_N = 3

# Trivial acknowledgements ("ok", "yes", "do it") yield 0-1 content words;
# a real question like "what are my roommates called" yields 2. The boundary
# sits at 2, not 3, or substantive short questions are silently skipped.
MIN_CONTENT_WORDS = 2

_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_]+")

STOPWORDS = frozenset({
    "a", "an", "and", "are", "as", "at", "be", "but", "by", "can", "do", "for",
    "from", "how", "i", "if", "in", "is", "it", "me", "my", "no", "not", "of",
    "ok", "okay", "on", "or", "so", "that", "the", "then", "this", "to", "up",
    "was", "we", "what", "when", "where", "why", "yes", "you", "your",
})


def content_words(text: str) -> list[str]:
    tokens = [token.lower() for token in _TOKEN_PATTERN.findall(text)]
    return [token for token in tokens if token not in STOPWORDS]


def _fts_query(text: str) -> str:
    """Build an FTS5 MATCH expression from the query's content words.

    Tokens are OR-ed, so stopwords must be dropped: a query like "where am I
    moving to next month" would otherwise match anything containing "to" or
    "month" and hand those hits rank 1, poisoning the RRF fusion with noise
    that outranks the genuinely relevant semantic matches.

    Quoting each token keeps punctuation from breaking MATCH syntax.
    """
    tokens = [token.lower() for token in _TOKEN_PATTERN.findall(text)]
    return " OR ".join(f'"{token}"' for token in content_words(text) or tokens)


def search(
    connection: sqlite3.Connection,
    query: str,
    limit: int = FINAL_K,
    scope: str | None = None,
    include_superseded: bool = False,
    parent: str | None = None,
    about_user: bool | None = None,
    exclude_ids: set[str] | None = None,
    node_type: str | None = None,
    max_per_source: int = MAX_PER_SOURCE,
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

    # NULL is the world stratum, not a third one: 'raw' and the preference types
    # never declare about_user, and filtering on `= 0` would drop them from both
    # populations and so from stratified retrieval entirely.
    about_clause = ""
    if about_user is True:
        about_clause = "AND n.about_user = 1"
    elif about_user is False:
        about_clause = "AND COALESCE(n.about_user, 0) = 0"

    # Excluded in SQL rather than filtered afterwards: dropping rows after LIMIT
    # would let already-loaded nodes consume result slots and silently shrink
    # what comes back.
    exclude_clause = ""
    exclude_params: tuple[Any, ...] = ()
    if exclude_ids:
        exclude_params = tuple(exclude_ids)
        exclude_clause = f"AND n.id NOT IN ({', '.join('?' * len(exclude_params))})"

    type_clause = ""
    type_params: tuple[Any, ...] = ()
    if node_type:
        type_clause = "AND n.type = ?"
        type_params = (node_type,)

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
        ),
        ranked AS (
            SELECT n.id, n.title, n.summary, n.type, n.scope, n.about_user,
                   n.window_start, n.window_end, n.stale,
                   lexical.rank  AS lexical_rank,
                   semantic.rank AS semantic_rank,
                   (CASE WHEN n.type = 'raw' THEN ? ELSE 1.0 END)
                 * (COALESCE(1.0 / (? + lexical.rank), 0)
                  + COALESCE(1.0 / (? + semantic.rank), 0)) AS score,
                   ROW_NUMBER() OVER (
                       PARTITION BY COALESCE(n.derived_from, n.id)
                       ORDER BY (CASE WHEN n.type = 'raw' THEN ? ELSE 1.0 END)
                              * (COALESCE(1.0 / (? + lexical.rank), 0)
                               + COALESCE(1.0 / (? + semantic.rank), 0)) DESC
                   ) AS source_position
            FROM nodes n
            LEFT JOIN lexical  ON lexical.node_rowid  = n.rowid
            LEFT JOIN semantic ON semantic.node_rowid = n.rowid
            WHERE (lexical.rank IS NOT NULL OR semantic.rank IS NOT NULL)
              AND n.scope IN ({scope_placeholders})
              AND {parent_clause}
              {superseded_clause}
              {about_clause}
              {exclude_clause}
              {type_clause}
        )
        SELECT id, title, summary, type, scope, about_user,
               window_start, window_end, stale,
               lexical_rank, semantic_rank, score
        FROM ranked
        WHERE source_position <= ?
        ORDER BY score DESC
        LIMIT ?
    """

    rows = connection.execute(
        sql,
        (match_expression, CANDIDATE_K, query_vector, CANDIDATE_K,
         RAW_DEMOTION, RRF_K, RRF_K, RAW_DEMOTION, RRF_K, RRF_K,
         *scopes, *parent_params, *exclude_params, *type_params,
         max_per_source, limit),
    ).fetchall()
    return [dict(row) for row in rows]


def t1_ids(connection: sqlite3.Connection, scope: str | None = None) -> set[str]:
    """Node ids already autoloaded, and so not worth retrieving again.

    Retrieval competes for a budget the autoload set has already spent. Returning
    a node that is verbatim in context costs slots and reads as corroboration —
    two independent sources agreeing — when it is one source counted twice.
    """
    return {node["id"] for node in assemble_t1(connection, scope=scope)}


def search_stratified(
    connection: sqlite3.Connection,
    query: str,
    personal_limit: int = 3,
    world_limit: int = 3,
    scope: str | None = None,
    exclude_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Retrieve personal and world nodes as separate populations.

    A single ranked list lets one population starve the other: with ~500 wiki
    nodes against a handful of personal ones, "where am I moving to next month"
    returned four unrelated wiki sections and pushed the apartment and move-in
    facts past rank 20. Personal facts are few and disproportionately important,
    so they get guaranteed slots rather than competing on raw score.
    """
    personal = search(connection, query, limit=personal_limit, scope=scope,
                      about_user=True, exclude_ids=exclude_ids)
    world = search(connection, query, limit=world_limit, scope=scope,
                   about_user=False, exclude_ids=exclude_ids)
    return sorted([*personal, *world], key=lambda hit: hit["score"], reverse=True)


# A raw chunk runs to MAX_SECTION_CHARS (1500), so three of them would inject
# ~4.5k characters into every message. Retrieved text is a pointer, not the
# document: the id travels with each hit, so an agent that wants the whole
# section can fetch it deliberately instead of paying for it on every turn.
HIT_CHARS = 400


def _date_field(node: dict[str, Any]) -> str:
    start, end = node.get("window_start"), node.get("window_end")
    if not start and not end:
        return "-"
    if start and end:
        return start if start == end else f"{start}..{end}"
    return f"{start}.." if start else f"..{end}"


def render_context(
    hits: list[dict[str, Any]], heading: str = "# memory that could be useful:"
) -> str:
    """Render hits as one comma-delimited row each: type, date, title, content.

    Deliberately bare. This text is injected on every qualifying message, so
    prose framing is a per-message tax; the standing instructions for reading it
    — including what a `raw` hit means — live once in the meta node instead.

    The title doubles as the handle: it is unique, so an agent that wants the
    whole node digs by the title printed here.
    """
    if not hits:
        return ""

    lines = [heading]
    for hit in hits:
        content = " ".join(hit["summary"].split())
        if len(content) > HIT_CHARS:
            content = content[:HIT_CHARS].rstrip() + "…"
        lines.append(f"{hit['type']}, {_date_field(hit)}, {hit['title']}, {content}")
    return "\n".join(lines)


DIG_FIELDS = (
    "title", "type", "summary", "about_user", "scope",
    "window_start", "window_end", "stale", "origin", "locator", "updated",
)


def dig(connection: sqlite3.Connection, title: str) -> dict[str, Any] | None:
    """Expand one node by its title, the handle retrieval prints.

    Retrieval shows a truncated line; this is how an agent reads the whole thing
    and sees what it connects to. Links resolve to titles rather than ids,
    because ids never appear in what the agent was shown — handing back a
    dangling slug would be a reference it cannot follow.
    """
    row = connection.execute(
        f"SELECT rowid, id, superseded_by, parent, {', '.join(DIG_FIELDS)} "
        "FROM nodes WHERE title = ?",
        (title,),
    ).fetchone()
    if row is None:
        return None

    def title_of(node_id: str | None) -> str | None:
        if not node_id:
            return None
        found = connection.execute(
            "SELECT title FROM nodes WHERE id = ?", (node_id,)
        ).fetchone()
        return found["title"] if found else None

    node = {field: row[field] for field in DIG_FIELDS}
    node["about_user"] = None if row["about_user"] is None else bool(row["about_user"])
    node["stale"] = bool(row["stale"])
    node["superseded_by"] = title_of(row["superseded_by"])
    node["parent"] = title_of(row["parent"])
    node["supersedes"] = [
        found["title"] for found in connection.execute(
            "SELECT title FROM nodes WHERE superseded_by = ?", (row["id"],)
        )
    ]
    node["edges"] = [
        {"rel": edge["rel"], "to": edge["title"]}
        for edge in connection.execute(
            "SELECT e.rel, n.title FROM node_edges AS e "
            "JOIN nodes AS n ON n.id = e.dst_id WHERE e.src_id = ?",
            (row["id"],),
        )
    ]
    return node


def render_dig(node: dict[str, Any] | None, title: str = "") -> str:
    """Render a dig result as the literal text the agent receives."""
    if node is None:
        return f"No memory titled {title!r}."

    lines = [f"# {node['title']}"]
    for field in ("type", "about_user", "scope", "origin", "stale", "updated"):
        if node[field] is not None:
            lines.append(f"{field}: {node[field]}")
    if node["window_start"] or node["window_end"]:
        lines.append(f"time: {_date_field(node)}")
    if node["locator"]:
        lines.append(f"source: {node['locator']}")
    for field in ("superseded_by", "parent"):
        if node[field]:
            lines.append(f"{field}: {node[field]}")
    if node["supersedes"]:
        lines.append(f"supersedes: {', '.join(node['supersedes'])}")
    for edge in node["edges"]:
        lines.append(f"{edge['rel']}: {edge['to']}")
    lines.extend(["", node["summary"]])
    return "\n".join(lines)


def should_retrieve(message: str, min_content_words: int = MIN_CONTENT_WORDS) -> bool:
    """Gate per-message auto-retrieval on query shape, not on score.

    RRF scores are not normalised — with rrf_k = 60 everything lands in a narrow
    band and there is no absolute meaning to "relevant enough". Counting content
    words needs no calibration and handles the "ok" / "yes" case that motivated
    the gate.
    """
    return len(content_words(message)) >= min_content_words


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

    # meta first: it is the instructions for reading everything under it, and a
    # reader who meets the facts before the rules has already read them wrong.
    ordered = sorted(by_type, key=lambda name: (name != "meta", name))

    lines = ["# Memory"]
    for node_type in ordered:
        lines.append(f"\n## {node_type}")
        for node in by_type[node_type]:
            window = ""
            if node.get("window_start") or node.get("window_end"):
                window = f" [{node.get('window_start') or ''}..{node.get('window_end') or ''}]"
            lines.append(f"- {node['summary']}{window}")
    return "\n".join(lines)
