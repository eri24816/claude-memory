"""The literal text an agent receives: T1, retrieved context, and a dig."""

from __future__ import annotations

import pytest

from claude_memory import db, retrieval, settings, store
from claude_memory.models import InvariantError

NODES = [
    {"claim": "Eric's apartment is 2442 Leslie Circle", "type": "fact",
     "about_user": True,
     "detail": "Traver Heights, near UMich North Campus. Shared with two "
               "roommates on a lease running to 2027-07-31.",
     "window_start": "2026-07-17", "window_end": "2027-07-31"},
    {"claim": "Eric moves in and collects keys", "type": "todo",
     "about_user": True,
     "window_start": "2026-08-05", "window_end": "2026-08-05"},
]


@pytest.fixture
def connection():
    conn = db.connect(":memory:")
    store.remember(conn, NODES)
    yield conn
    conn.close()


def test_prefs_lead_the_autoload_block(connection):
    """The pref files are the instructions for reading everything under them; a
    reader who meets the facts first has already read them wrong."""
    settings.write("conv", "# Behaviour\n- Be terse.")

    block = retrieval.render_t1(retrieval.assemble_t1(connection))
    meta_heading = settings.read("meta").splitlines()[0].lstrip("# ")
    assert block.index(meta_heading) < block.index("# Behaviour")
    assert block.index("# Behaviour") < block.index("## fact")


def test_code_conventions_are_not_preloaded(connection):
    """code.md is the largest of the three files and irrelevant to every session
    that writes no code; it is reached through the code-prefs skill instead."""
    settings.write("code", "# Conventions\n- Never use tabs.")
    assert "Never use tabs" not in retrieval.render_t1(
        retrieval.assemble_t1(connection)
    )


def test_editorial_comments_do_not_reach_context(connection):
    """These files are edited by hand and by the agent, so they collect notes
    about themselves. Worth keeping in the file, not worth paying for per
    session."""
    settings.write("conv", "# Behaviour\n<!-- TODO: merge these -->\n- Be terse.")
    block = retrieval.render_t1(retrieval.assemble_t1(connection))
    assert "Be terse" in block
    assert "TODO" not in block


def test_context_rows_are_type_date_claim(connection):
    hits = retrieval.search(connection, "apartment Leslie", limit=1,
                            exclude_ids=None)
    block = retrieval.render_context(hits)

    assert block.startswith("# memory that could be useful:")
    kind, date, claim = (part.strip() for part in block.splitlines()[1].split(",", 2))
    assert kind == "fact"
    assert date == "2026-07-17..2027-07-31"
    assert claim.startswith("Eric's apartment is 2442 Leslie Circle")


def test_rows_are_never_truncated(connection):
    """v0 clipped a 684-character summary at 400 and had to instruct the agent to
    dig whenever a row 'looked like it mattered'. A claim is complete by
    construction, so dig stops being repair work for the renderer."""
    hits = retrieval.search(connection, "apartment Leslie", exclude_ids=None)
    assert "…" not in retrieval.render_context(hits)


def test_detail_is_marked_but_not_rendered(connection):
    """An 8-word claim reads as the whole node. Without the marker an agent would
    act on the summary of a node whose entire value sits in unread detail."""
    hits = retrieval.search(connection, "apartment Leslie", limit=1,
                            exclude_ids=None)
    row = retrieval.render_context(hits).splitlines()[1]

    assert row.endswith("+")
    assert "Traver Heights" not in row
    assert "Traver Heights" in retrieval.render_dig(
        retrieval.dig(connection, "Eric's apartment is 2442 Leslie Circle")
    )


def test_claimless_nodes_carry_no_marker(connection):
    node = retrieval.dig(connection, "Eric moves in and collects keys")
    assert node["detail"] is None
    assert not retrieval.render_hit(node).endswith("+")


def test_context_carries_no_standing_instructions(connection):
    """Guidance is a per-message tax when it is injected per message; the rules
    for reading these rows live once in meta.md."""
    block = retrieval.render_context(
        retrieval.search(connection, "apartment", exclude_ids=None)
    )
    assert "supersede" not in block.lower()
    assert "dig" not in block.lower()


def test_claims_need_not_be_unique(connection):
    """v0 made the handle unique and so had to suffix collisions with '(2)'. That
    text is now rendered verbatim into context, where a suffix is meaningless as
    English -- and two nodes may legitimately assert the same thing."""
    store.remember(connection, [
        {"claim": "Eric moves in and collects keys", "type": "action",
         "about_user": True, "window_start": "2026-08-05"},
    ])
    claims = [row["claim"] for row in connection.execute(
        "SELECT claim FROM nodes WHERE claim LIKE 'Eric moves in%'"
    )]
    assert claims == ["Eric moves in and collects keys"] * 2


def test_ambiguity_is_not_absence(connection):
    """Reporting 'nothing matched' when several things did leads the agent to
    write a duplicate, which an append-only store can never merge."""
    store.remember(connection, [
        {"claim": "Eric moves in and collects keys", "type": "action",
         "about_user": True, "window_start": "2026-08-05"},
    ])
    result = retrieval.dig(connection, "collects keys")
    assert "matches 2 nodes" in result["error"]
    assert "matches 2 nodes" in retrieval.render_dig(result, "collects keys")


def test_supersession_keeps_the_id_stable(connection):
    """The handle is the id, which never moves, so a supersession is an insert
    plus a pointer -- no title has to be handed over to the successor."""
    store.remember(connection, [
        {"op": "supersede", "supersedes": "eric-s-apartment-is-2442-leslie-circle",
         "claim": "Eric renewed the Leslie Circle lease", "type": "fact",
         "about_user": True, "window_start": "2027-08-01"},
    ])
    old = retrieval.dig(connection, "eric-s-apartment-is-2442-leslie-circle")
    assert old["superseded_by"] == "Eric renewed the Leslie Circle lease"

    new = retrieval.dig(connection, "Eric renewed the Leslie Circle lease")
    assert new["stale"] is False
    assert new["supersedes"] == ["Eric's apartment is 2442 Leslie Circle"]


def test_write_operations_accept_claims(connection):
    store.set_stale(connection, "Eric moves in and collects keys")
    assert retrieval.dig(connection, "Eric moves in and collects keys")["stale"]


def test_unknown_handle_is_reported_by_name(connection):
    with pytest.raises(InvariantError, match="no node matching"):
        store.set_stale(connection, "No such memory")


def test_dig_renders_the_id_it_resolved(connection):
    """Search prints claims, not ids, to keep rows cheap -- so dig has to hand
    back the unambiguous handle for whatever comes next."""
    block = retrieval.render_dig(
        retrieval.dig(connection, "Eric moves in and collects keys")
    )
    assert block.startswith("# Eric moves in and collects keys")
    assert "id: eric-moves-in-and-collects-keys" in block
    assert "type: todo" in block
    assert "time: 2026-08-05" in block


def test_digs_batch_into_one_result(connection):
    """A tool call is billed for its cached prefix once per call, so N digs in
    one call cost a fraction of N calls."""
    handles = ["Eric moves in and collects keys",
               "Eric's apartment is 2442 Leslie Circle"]
    block = retrieval.render_digs(
        [(h, retrieval.dig(connection, h)) for h in handles]
    )
    assert block.count("id: ") == 2
    assert "---" in block


def test_dig_misses_report_the_handle(connection):
    assert retrieval.dig(connection, "No such memory") is None
    assert "No such memory" in retrieval.render_dig(None, "No such memory")
