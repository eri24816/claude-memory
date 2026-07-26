"""Reads: hybrid BM25 + vector retrieval, and the categorical T1 assembly."""

from __future__ import annotations

import re
import sqlite3
from typing import Any

from . import embed, transcript

RRF_K = 60
CANDIDATE_K = 200
FINAL_K = 10

# v0 demoted raw hits by 0.6 so a refined node covering the same ground would
# win. Refinement never once happened — 518 raw nodes, zero refined — so there
# was no counterpart to lose to and the penalty only made raw hits weaker at the
# one thing they were good for.
#
# 0.2.0 caps instead. A cap bounds the flood regardless of corpus size, and it
# does not care about rank: the best two raw hits still arrive at full strength
# and in their earned position, while the third onwards cannot crowd out typed
# nodes no matter how many chunks the corpus holds. The demotion tried to answer
# "is this chunk worth less than a claim?", which needs a refined counterpart to
# be answerable at all. The cap answers "how much of one query may be chunks?",
# which is answerable with nothing in the store but chunks.
RAW_PER_QUERY = 2

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
    raw_limit: int = RAW_PER_QUERY,
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

    # NULL is the world stratum, not a third one: `idea` never declares
    # about_user, and filtering on `= 0` would drop it from both populations and
    # so from stratified retrieval entirely.
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
            SELECT n.id, n.claim, n.detail, n.type, n.scope, n.about_user,
                   n.window_start, n.window_end, n.stale,
                   lexical.rank  AS lexical_rank,
                   semantic.rank AS semantic_rank,
                   COALESCE(1.0 / (? + lexical.rank), 0)
                 + COALESCE(1.0 / (? + semantic.rank), 0) AS score,
                   ROW_NUMBER() OVER (
                       PARTITION BY COALESCE(n.derived_from, n.id)
                       ORDER BY COALESCE(1.0 / (? + lexical.rank), 0)
                              + COALESCE(1.0 / (? + semantic.rank), 0) DESC
                   ) AS source_position,
                   ROW_NUMBER() OVER (
                       PARTITION BY n.type = 'raw'
                       ORDER BY COALESCE(1.0 / (? + lexical.rank), 0)
                              + COALESCE(1.0 / (? + semantic.rank), 0) DESC
                   ) AS raw_position
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
        SELECT id, claim, detail, type, scope, about_user,
               window_start, window_end, stale,
               lexical_rank, semantic_rank, score
        FROM ranked
        WHERE source_position <= ?
          AND (type != 'raw' OR raw_position <= ?)
        ORDER BY score DESC
        LIMIT ?
    """

    # Filtered before LIMIT, not after. Trimming raw hits from a finished result
    # set would let capped-away chunks consume slots and hand back four rows for
    # a limit of ten -- the cap has to shrink what raw may occupy, not what the
    # caller receives.
    #
    # An explicit `--type raw` opts out: the cap protects a mixed result set from
    # being swamped, and someone who asked for chunks and nothing else is not
    # being swamped, they are being answered.
    effective_raw_limit = limit if node_type == "raw" else raw_limit

    rows = connection.execute(
        sql,
        (match_expression, CANDIDATE_K, query_vector, CANDIDATE_K,
         RRF_K, RRF_K, RRF_K, RRF_K, RRF_K, RRF_K,
         *scopes, *parent_params, *exclude_params, *type_params,
         max_per_source, effective_raw_limit, limit),
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


# Appended to a rendered claim whose node carries detail. Without it, digging is
# a gamble: an 8-word claim reads as the whole node, so an agent would act on the
# summary of a node whose entire value sits in the detail it never saw. One
# character turns that into an informed choice.
DETAIL_MARKER = " +"


def _date_field(node: dict[str, Any]) -> str:
    start, end = node.get("window_start"), node.get("window_end")
    if not start and not end:
        return "-"
    if start and end:
        return start if start == end else f"{start}..{end}"
    return f"{start}.." if start else f"..{end}"


def render_hit(node: dict[str, Any]) -> str:
    """One node as one line: type, window, claim, and whether detail exists."""
    marker = DETAIL_MARKER if node.get("detail") else ""
    return f"{node['type']}, {_date_field(node)}, {node['claim']}{marker}"


def render_context(
    hits: list[dict[str, Any]], heading: str = "# memory that could be useful:"
) -> str:
    """Render hits as one comma-delimited row each: type, date, claim.

    Nothing is truncated any more, and that is the point. v0 clipped a 684-char
    summary at 400 and had to instruct the agent to dig "whenever a truncated row
    looks like it matters" — dig was repair work for a render that could not tell
    the whole truth. A claim is complete by construction, so a row is now either
    the whole node or a claim plus a `+` saying there is more.

    Deliberately bare: this text is injected on every qualifying message, so
    prose framing is a per-message tax.
    """
    if not hits:
        return ""
    return "\n".join([heading, *(render_hit(hit) for hit in hits)])


DIG_FIELDS = (
    "claim", "type", "detail", "about_user", "scope",
    "window_start", "window_end", "stale", "origin", "locator",
    "source_session", "updated",
)


# How many siblings to show on each side of the dug node. Enough to see what the
# session was doing around this node, few enough that a dig stays a dig.
SESSION_RADIUS = 3


def _session_neighbourhood(
    connection: sqlite3.Connection,
    node_id: str,
    session_id: str | None,
    radius: int = SESSION_RADIUS,
) -> dict[str, Any] | None:
    """The nodes this session wrote, in order, centred on this one.

    A node is rarely the whole of what a session learned -- it is one of a
    handful written in the same stretch of work, and the others are its context.
    Retrieval can only surface what matches the query, so a node whose siblings
    explain it is unreachable from the sibling that scored. This gives the agent
    a way to walk sideways instead.

    Ordered by rowid, which is insertion order: the sequence the session left
    them in, not the sequence they were edited into.
    """
    if not session_id:
        return None

    rows = connection.execute(
        "SELECT id, claim, type FROM nodes WHERE source_session = ? ORDER BY rowid",
        (session_id,),
    ).fetchall()
    identifiers = [row["id"] for row in rows]
    if node_id not in identifiers or len(rows) < 2:
        # Alone in its session, so there is no neighbourhood to show -- the
        # `session:` line already says which session it was.
        return None

    index = identifiers.index(node_id)
    start = max(0, index - radius)
    end = min(len(rows), index + radius + 1)
    return {
        "position": index + 1,
        "total": len(rows),
        "before_truncated": start > 0,
        "after_truncated": end < len(rows),
        "nodes": [
            {"id": row["id"], "claim": row["claim"], "type": row["type"],
             "current": row["id"] == node_id}
            for row in rows[start:end]
        ],
    }


def dig(connection: sqlite3.Connection, handle: str) -> dict[str, Any] | None:
    """Expand one node by id, exact claim, or unambiguous claim fragment.

    Links render as the linked node's claim rather than its id: the claim is
    what the agent has seen, and `resolve` accepts it, so every reference in a
    dig result is one it can follow straight back in.
    """
    from .models import AmbiguousHandle, InvariantError
    from .store import resolve

    try:
        node_id = resolve(connection, handle)
    except AmbiguousHandle as error:
        # Ambiguity is not absence. Collapsing the two tells the agent nothing
        # matched when in fact several did, and the natural next move -- write a
        # new node, because memory apparently lacks this -- creates the
        # duplicate the store can never merge.
        return {"error": str(error)}
    except InvariantError:
        return None

    row = connection.execute(
        f"SELECT rowid, id, superseded_by, parent, {', '.join(DIG_FIELDS)} "
        "FROM nodes WHERE id = ?",
        (node_id,),
    ).fetchone()
    if row is None:
        return None

    def claim_of(other_id: str | None) -> str | None:
        if not other_id:
            return None
        found = connection.execute(
            "SELECT claim FROM nodes WHERE id = ?", (other_id,)
        ).fetchone()
        return found["claim"] if found else None

    node = {field: row[field] for field in DIG_FIELDS}
    node["id"] = row["id"]
    node["about_user"] = None if row["about_user"] is None else bool(row["about_user"])
    node["stale"] = bool(row["stale"])
    node["superseded_by"] = claim_of(row["superseded_by"])
    node["parent"] = claim_of(row["parent"])
    node["supersedes"] = [
        found["claim"] for found in connection.execute(
            "SELECT claim FROM nodes WHERE superseded_by = ?", (row["id"],)
        )
    ]
    node["edges"] = [
        {"rel": edge["rel"], "to": edge["claim"]}
        for edge in connection.execute(
            "SELECT e.rel, n.claim FROM node_edges AS e "
            "JOIN nodes AS n ON n.id = e.dst_id WHERE e.src_id = ?",
            (row["id"],),
        )
    ]
    node["session_nodes"] = _session_neighbourhood(
        connection, row["id"], row["source_session"]
    )
    return node


def render_dig(node: dict[str, Any] | None, handle: str = "") -> str:
    """Render a dig result as the literal text the agent receives."""
    if node is None:
        return f"No memory matching {handle!r}."
    if "error" in node:
        return f"# {handle}\n{node['error']}"

    lines = [f"# {node['claim']}", f"id: {node['id']}"]
    for field in ("type", "about_user", "scope", "origin", "stale", "updated"):
        if node[field] is not None:
            lines.append(f"{field}: {node[field]}")
    if node["window_start"] or node["window_end"]:
        lines.append(f"time: {_date_field(node)}")
    if node["locator"]:
        lines.append(f"source: {node['locator']}")
    if node["source_session"]:
        lines.append(f"session: {node['source_session']}")
    for field in ("superseded_by", "parent"):
        if node[field]:
            lines.append(f"{field}: {node[field]}")
    if node["supersedes"]:
        lines.append(f"supersedes: {', '.join(node['supersedes'])}")
    for edge in node["edges"]:
        lines.append(f"{edge['rel']}: {edge['to']}")
    if node["detail"]:
        lines.extend(["", node["detail"]])
    lines.extend(render_session_neighbourhood(node.get("session_nodes")))
    return "\n".join(lines)


def render_session_neighbourhood(neighbourhood: dict[str, Any] | None) -> list[str]:
    """The session's other nodes, in order, with this one marked.

    Rendered as claims rather than ids because `dig` accepts a claim: every line
    here is one the agent can dig straight back in, so the block is a set of
    moves and not just context.
    """
    if not neighbourhood:
        return []

    lines = ["", f"session wrote {neighbourhood['total']} nodes; "
                 f"this is #{neighbourhood['position']}:"]
    if neighbourhood["before_truncated"]:
        lines.append("  ...")
    for sibling in neighbourhood["nodes"]:
        marker = "->" if sibling["current"] else "  "
        lines.append(f"{marker}{sibling['claim']}")
    if neighbourhood["after_truncated"]:
        lines.append("  ...")
    return lines


def render_digs(nodes: list[tuple[str, dict[str, Any] | None]]) -> str:
    """Several digs in one result.

    A tool call re-sends the cached prefix and is billed for it once per call,
    so N separate digs cost N times what one batched call costs. Multi-node
    reads are the normal case — a search hands back several candidate rows — so
    batching is the default shape rather than an optimisation.
    """
    return "\n\n---\n\n".join(render_dig(node, handle) for handle, node in nodes)


def dig_context(
    connection: sqlite3.Connection, title: str, window: int = 3
) -> dict[str, Any] | None:
    """The transcript excerpt a node's source points at.

    dig() prints locator/source_session as bare ids -- a jump-to-context
    anchor an agent could always follow by hand, but nothing before this did
    the following. Only resolves session-sourced nodes: a derived (ingested)
    node's locator is a `file#anchor` into a wiki file, and its detail is
    already that file's text verbatim, so there is nothing further to fetch.
    """
    from .models import InvariantError
    from .store import resolve

    try:
        node_id = resolve(connection, title)
    except InvariantError:
        return None
    row = connection.execute(
        "SELECT locator, source_session FROM nodes WHERE id = ?", (node_id,)
    ).fetchone()
    if row is None or not row["locator"] or not row["source_session"]:
        return None
    return transcript.excerpt(row["source_session"], row["locator"], window=window)


def render_transcript(excerpt: dict[str, Any] | None, title: str = "") -> str:
    if excerpt is None:
        return f"No resolvable transcript context for {title!r}."
    lines = [f"# transcript around {title!r}", excerpt["path"], ""]
    for message in excerpt["messages"]:
        marker = ">>> " if message["is_hit"] else ""
        lines.append(f"{marker}[{message['role']}] {message['text']}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


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
        SELECT id, claim, detail, type, scope, window_start, window_end
        FROM nodes
        WHERE superseded_by IS NULL
          AND scope IN ({scope_placeholders})
          AND (
                (about_user = 1 AND type = 'fact'
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
                SELECT id, claim, detail, type, scope, window_start, window_end
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
    """The autoload block injected at session start: prefs whole, nodes as claims.

    Two populations with opposite economics, so they are rendered by opposite
    rules. The pref files are bounded and behavioural — a rule the agent has to
    fetch is a rule it will not follow — so they go in verbatim. Nodes grow
    without limit and are looked up on demand, so they contribute one line each
    and pay for their detail only when something digs.
    """
    from . import settings

    sections = []
    prefs = settings.render()
    if prefs:
        # Before the nodes: these are the instructions for reading everything
        # under them, and a reader who meets the facts first has already read
        # them wrong.
        sections.append(prefs)

    by_type: dict[str, list[dict[str, Any]]] = {}
    for node in nodes:
        by_type.setdefault(node["type"], []).append(node)

    for node_type in sorted(by_type):
        lines = [f"## {node_type}"]
        for node in by_type[node_type]:
            window = ""
            if node.get("window_start") or node.get("window_end"):
                window = f" [{node.get('window_start') or ''}..{node.get('window_end') or ''}]"
            marker = DETAIL_MARKER if node.get("detail") else ""
            lines.append(f"- {node['claim']}{marker}{window}")
        sections.append("\n".join(lines))

    if not sections:
        return ""
    return "\n\n".join(["# Memory", *sections])
