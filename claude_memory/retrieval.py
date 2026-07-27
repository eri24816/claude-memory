"""Reads: hybrid BM25 + vector retrieval, and the categorical T1 assembly."""

from __future__ import annotations

import datetime as dt
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
RAW_PER_QUERY = 5

# Sections of one document are near-identical in embedding space, so a matching
# page sweeps the top slots and buries every other source. Capping per source
# document costs a little depth on the best match and buys breadth across the
# corpus, which is what a memory lookup is for.
MAX_PER_SOURCE = 2

LEAD_TIME_DAYS = 30
RECENT_N = 3

# How many recently written nodes T1 shows as history. Eight is enough to cover
# the last session or two of work and short enough to read at a glance.
HISTORY_N = 8

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
                   n.window_start, n.window_end, n.stale, n.superseded_by,
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
              {type_clause}
        )
        SELECT id, claim, detail, type, scope, about_user,
               window_start, window_end, stale, superseded_by,
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
         *scopes, *parent_params, *type_params,
         max_per_source, effective_raw_limit, limit),
    ).fetchall()

    hits = []
    for row in rows:
        hit = dict(row)
        # Costs a query only when a superseded node was asked for: the default
        # search excludes them, so this loop is a no-op on the common path.
        hit["correction"] = newest_correction(connection, hit["superseded_by"])
        hits.append(hit)
    return hits


def search_stratified(
    connection: sqlite3.Connection,
    query: str,
    personal_limit: int = 3,
    world_limit: int = 3,
    scope: str | None = None,
) -> list[dict[str, Any]]:
    """Retrieve personal and world nodes as separate populations.

    A single ranked list lets one population starve the other: with ~500 wiki
    nodes against a handful of personal ones, "where am I moving to next month"
    returned four unrelated wiki sections and pushed the apartment and move-in
    facts past rank 20. Personal facts are few and disproportionately important,
    so they get guaranteed slots rather than competing on raw score.
    """
    personal = search(connection, query, limit=personal_limit, scope=scope,
                      about_user=True)
    world = search(connection, query, limit=world_limit, scope=scope,
                   about_user=False)
    return sorted([*personal, *world], key=lambda hit: hit["score"], reverse=True)


# Marks a node that carries detail. Without it, digging is a gamble: an 8-word
# claim reads as the whole node, so an agent would act on the summary of a node
# whose entire value sits in the detail it never saw. One character turns that
# into an informed choice.
DETAIL_MARKER = "+"

ROW_SEPARATOR = "|"


def short_date(value: str, today: dt.date | None = None) -> str:
    """`mm-dd` inside the current year, `yy-mm-dd` outside it.

    The century is never in question and the current year is the overwhelming
    common case, so spending four characters on "2026-" buys nothing. Dropping
    the year entirely would be worse than useless -- a date that silently means
    a different year every January -- so a year that is *not* the current one
    keeps two digits of it, and reads as visibly not-this-year.
    """
    today = today or dt.date.today()
    year, month, day = value.split("-")
    if year == f"{today.year}":
        return f"{month}-{day}"
    return f"{year[2:]}-{month}-{day}"


def _row_window(node: dict[str, Any]) -> str:
    start, end = node.get("window_start"), node.get("window_end")
    if not start and not end:
        return ""
    if start and end:
        return (
            short_date(start) if start == end
            else f"{short_date(start)}..{short_date(end)}"
        )
    return short_date(start) if start else f"..{short_date(end)}"


def render_row(node: dict[str, Any]) -> str:
    """One node as one line, the same everywhere a claim is listed.

        <claim>|<window>|<detail mark>|-> <newest correction>

    One format for T1, search, the per-message hook and dig's session block, so
    an agent learns to read a row once. Every field after the claim is a
    decision aid rather than content: whether there is more to fetch, when it
    was true, and whether something has since replaced it -- the three questions
    that otherwise need a dig to answer.

    Trailing empty fields are dropped. Interior ones are kept, so the position
    of a field never depends on whether an earlier one was populated; keeping
    the trailing ones too would tax every T1 row for nodes that have neither
    detail nor a correction, which is most of them.
    """
    fields = [
        node["claim"],
        _row_window(node),
        DETAIL_MARKER if node.get("detail") else "",
        f"-> {node['correction']}" if node.get("correction") else "",
    ]
    while len(fields) > 1 and not fields[-1]:
        fields.pop()
    return ROW_SEPARATOR.join(fields)


def newest_correction(
    connection: sqlite3.Connection, superseded_by: str | None
) -> str | None:
    """Follow the supersession chain to the claim that currently stands.

    The immediate successor may itself have been superseded, and a row pointing
    at a correction that was later corrected sends the reader one hop short of
    what is true. Cycles cannot be written -- a node may not supersede itself --
    but the guard is cheap and a malformed store should not hang a render.
    """
    seen: set[str] = set()
    claim: str | None = None
    current = superseded_by
    while current and current not in seen:
        seen.add(current)
        row = connection.execute(
            "SELECT claim, superseded_by FROM nodes WHERE id = ?", (current,)
        ).fetchone()
        if row is None:
            break
        claim = row["claim"]
        current = row["superseded_by"]
    return claim


def render_context(
    hits: list[dict[str, Any]], heading: str = "# memory that could be useful:"
) -> str:
    """Render hits in the standard row format.

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
    return "\n".join([heading, *(render_row(hit) for hit in hits)])


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
        "SELECT id, claim, type, detail, superseded_by, window_start, window_end "
        "FROM nodes WHERE source_session = ? ORDER BY rowid",
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
             "detail": row["detail"],
             "window_start": row["window_start"], "window_end": row["window_end"],
             # The session block is the one listing that shows superseded nodes,
             # so it is the one place the correction pointer earns its keep.
             "correction": newest_correction(connection, row["superseded_by"]),
             "current": row["id"] == node_id}
            for row in rows[start:end]
        ],
    }


def reference(
    connection: sqlite3.Connection, node_id: str | None
) -> dict[str, Any] | None:
    """A node named by another node, carrying what the row format needs.

    Every reference a dig prints -- the successor, the parent, what this
    supersedes, what it links to -- is another node, and a bare claim tells the
    reader nothing about whether it is worth following. As a row it says whether
    there is detail behind it and whether it has itself been replaced, which is
    the difference between one dig and three.
    """
    if not node_id:
        return None
    row = connection.execute(
        "SELECT id, claim, detail, window_start, window_end, superseded_by "
        "FROM nodes WHERE id = ?",
        (node_id,),
    ).fetchone()
    if row is None:
        return None
    return {
        "id": row["id"],
        "claim": row["claim"],
        "detail": row["detail"],
        "window_start": row["window_start"],
        "window_end": row["window_end"],
        "correction": newest_correction(connection, row["superseded_by"]),
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

    node = {field: row[field] for field in DIG_FIELDS}
    node["id"] = row["id"]
    node["about_user"] = None if row["about_user"] is None else bool(row["about_user"])
    node["stale"] = bool(row["stale"])
    node["superseded_by"] = reference(connection, row["superseded_by"])
    node["parent"] = reference(connection, row["parent"])
    node["supersedes"] = [
        reference(connection, found["id"]) for found in connection.execute(
            "SELECT id FROM nodes WHERE superseded_by = ?", (row["id"],)
        )
    ]
    node["edges"] = [
        {"rel": edge["rel"], "to": reference(connection, edge["dst_id"])}
        for edge in connection.execute(
            "SELECT rel, dst_id FROM node_edges WHERE src_id = ?", (row["id"],)
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
        lines.append(f"time: {_row_window(node)}")
    if node["locator"]:
        lines.append(f"source: {node['locator']}")
    if node["source_session"]:
        lines.append(f"session: {node['source_session']}")
    # Every reference is a row, one per line: rows carry `|`, so comma-joining
    # several of them onto one line would leave the reader parsing punctuation
    # to find where one node ends and the next begins.
    for field in ("superseded_by", "parent"):
        if node[field]:
            lines.append(f"{field}: {render_row(node[field])}")
    for superseded in node["supersedes"]:
        lines.append(f"supersedes: {render_row(superseded)}")
    for edge in node["edges"]:
        if edge["to"]:
            lines.append(f"{edge['rel']}: {render_row(edge['to'])}")
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
        lines.append(f"{marker}{render_row(sibling)}")
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


def session_history(
    connection: sqlite3.Connection,
    scope: str | None = None,
    limit: int = HISTORY_N,
) -> list[dict[str, Any]]:
    """The last nodes written, oldest first: what the store was just doing.

    The categorical set answers "what is true about the user"; nothing in it
    answers "what were we working on", because a session's own output is spread
    across types and mostly capped away by `recent_n`. A session that opens
    without it starts from the same standing facts every time and re-derives the
    thread of work from whatever the user happens to say first.

    Ordered by rowid, which is insertion order -- the sequence the sessions left
    behind, not the sequence nodes were last edited into. `raw` is excluded:
    ingest writes hundreds of chunks in one pass, and a single wiki would be the
    entire history. Superseded nodes stay, carrying the `->` pointer to what
    replaced them: a history that hides the corrections hides its best signal.
    """
    scopes = ["global"] + ([scope] if scope and scope != "global" else [])
    scope_placeholders = ", ".join("?" * len(scopes))
    rows = connection.execute(
        f"""
        SELECT id, claim, detail, type, scope, window_start, window_end,
               superseded_by
        FROM nodes
        WHERE type != 'raw'
          AND scope IN ({scope_placeholders})
        ORDER BY rowid DESC
        LIMIT ?
        """,
        (*scopes, limit),
    ).fetchall()
    history = []
    for row in reversed(rows):
        node = dict(row)
        node["correction"] = newest_correction(connection, node["superseded_by"])
        history.append(node)
    return history


def render_t1(
    nodes: list[dict[str, Any]], history: list[dict[str, Any]] | None = None
) -> str:
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
            lines.append(f"- {render_row(node)}")
        sections.append("\n".join(lines))

    # Last, and labelled as a sequence rather than a set: everything above is
    # what currently holds, and these are events in order, some of which the
    # sections above have already superseded.
    if history:
        lines = [f"## history — the last {len(history)} nodes written, oldest first"]
        lines.extend(f"- {render_row(node)}" for node in history)
        sections.append("\n".join(lines))

    if not sections:
        return ""
    return "\n\n".join(["# Memory", *sections])
