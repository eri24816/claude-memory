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
               lexical.rank  AS lexical_rank,
               semantic.rank AS semantic_rank,
               (CASE WHEN n.type = 'raw' THEN ? ELSE 1.0 END)
             * (COALESCE(1.0 / (? + lexical.rank), 0)
              + COALESCE(1.0 / (? + semantic.rank), 0)) AS score
        FROM nodes n
        LEFT JOIN lexical  ON lexical.node_rowid  = n.rowid
        LEFT JOIN semantic ON semantic.node_rowid = n.rowid
        WHERE (lexical.rank IS NOT NULL OR semantic.rank IS NOT NULL)
          AND n.scope IN ({scope_placeholders})
          AND {parent_clause}
          {superseded_clause}
          {about_clause}
          {exclude_clause}
        ORDER BY score DESC
        LIMIT ?
    """

    rows = connection.execute(
        sql,
        (match_expression, CANDIDATE_K, query_vector, CANDIDATE_K,
         RAW_DEMOTION, RRF_K, RRF_K,
         *scopes, *parent_params, *exclude_params, limit),
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


REFINE_HINT = (
    "Some hits above are type 'raw': unread document text, chunked on headings "
    "and indexed without anyone judging what it claims. If one of them actually "
    "answered the question, that is worth keeping properly — extract the claim "
    "as a typed node and supersede the raw chunk with it "
    "(op='supersede', supersedes=<raw id>). Only do this for chunks you read and "
    "used; leaving the rest raw is the correct outcome."
)


def refine_hint(hits: list[dict[str, Any]]) -> str:
    """Prompt the agent to upgrade raw chunks it just found useful.

    Refinement is driven by retrieval rather than by a background pass: a chunk
    is worth a model's attention exactly when a real question has pulled it up,
    and at that moment the agent has the question in hand to extract against.
    """
    return REFINE_HINT if any(hit["type"] == "raw" for hit in hits) else ""


# A raw chunk runs to MAX_SECTION_CHARS (1500), so three of them would inject
# ~4.5k characters into every message. Retrieved text is a pointer, not the
# document: the id travels with each hit, so an agent that wants the whole
# section can fetch it deliberately instead of paying for it on every turn.
HIT_CHARS = 400


def render_context(
    hits: list[dict[str, Any]], heading: str = "# Memory — retrieved for this message"
) -> str:
    """Render retrieved hits as the literal text an agent receives.

    Ids are included because retrieval is also the refinement path: an agent that
    reads a raw chunk needs its id to supersede it.
    """
    if not hits:
        return ""

    lines = [heading, ""]
    for hit in hits:
        summary = " ".join(hit["summary"].split())
        if len(summary) > HIT_CHARS:
            summary = summary[:HIT_CHARS].rstrip() + "… (truncated)"
        window = ""
        if hit.get("window_start") or hit.get("window_end"):
            window = f" [{hit.get('window_start') or ''}..{hit.get('window_end') or ''}]"
        title = f"**{hit['title']}** — " if hit.get("title") else ""
        lines.append(f"- `{hit['type']}` {title}{summary}{window}  \n  `{hit['id']}`")

    hint = refine_hint(hits)
    if hint:
        lines.extend(["", hint])
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

    lines = ["# Memory"]
    for node_type in sorted(by_type):
        lines.append(f"\n## {node_type}")
        for node in by_type[node_type]:
            window = ""
            if node.get("window_start") or node.get("window_end"):
                window = f" [{node.get('window_start') or ''}..{node.get('window_end') or ''}]"
            lines.append(f"- {node['summary']}{window}")
    return "\n".join(lines)
