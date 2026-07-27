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


def test_history_is_the_last_nodes_written_oldest_first(connection):
    """The categorical set says what is true about the user; nothing in it says
    what was being worked on, and `recent_n` caps away most of a session's own
    output."""
    store.remember(connection, [
        {"claim": "Eric asked about the inspector", "type": "action",
         "about_user": True},
        {"claim": "The nodes tab pages at 200", "type": "fact",
         "about_user": False},
    ])
    history = retrieval.session_history(connection, limit=2)

    assert [node["claim"] for node in history] == [
        "Eric asked about the inspector",
        "The nodes tab pages at 200",
    ]


def test_history_carries_corrections_rather_than_hiding_them(connection):
    """A history that drops the superseded node drops the correction with it,
    which is the most useful thing a sequence of claims can show."""
    store.remember(connection, [{
        "op": "supersede", "supersedes": "eric-moves-in-and-collects-keys",
        "claim": "Eric moved in a day late", "type": "action",
        "about_user": True, "window_start": "2026-08-06",
    }])
    block = retrieval.render_t1(
        retrieval.assemble_t1(connection),
        retrieval.session_history(connection),
    )

    assert (
        "Eric moves in and collects keys|08-05||-> Eric moved in a day late"
        in block
    )


def test_history_excludes_ingested_chunks(connection):
    """One `ingest` writes hundreds of chunks in a single pass, so a wiki would
    otherwise be the entire history of the store."""
    store.remember(connection, [
        {"claim": "wiki.md#Cards#First card", "type": "raw",
         "detail": "Open a secured card first.", "origin": "derived",
         "about_user": None},
    ])
    history = retrieval.session_history(connection)

    assert all(node["type"] != "raw" for node in history)
    assert history[-1]["claim"] == "Eric moves in and collects keys"


def test_history_is_labelled_as_a_sequence_and_comes_last(connection):
    """Everything above it is what currently holds; these are events in order,
    some already superseded by the sections above."""
    block = retrieval.render_t1(
        retrieval.assemble_t1(connection),
        retrieval.session_history(connection),
    )

    assert block.index("## fact") < block.index("## history")
    assert "oldest first" in block


def test_context_rows_use_the_standard_row_format(connection):
    """claim|window|detail|-> correction, the same shape T1 and dig use."""
    hits = retrieval.search(connection, "apartment Leslie", limit=1)
    block = retrieval.render_context(hits)

    assert block.startswith("# memory that could be useful:")
    claim, window, detail = block.splitlines()[1].split("|")
    assert claim == "Eric's apartment is 2442 Leslie Circle"
    # 2026 is the fixture's current year; 2027 is not, so it keeps two digits.
    assert window == "07-17..27-07-31"
    assert detail == "+"


def test_a_row_drops_trailing_empty_fields_but_not_interior_ones(connection):
    """Field position must not depend on whether an earlier field was filled."""
    dated_no_detail = retrieval.render_row(
        {"claim": "A claim", "window_start": "2026-07-17", "window_end": None}
    )
    assert dated_no_detail == "A claim|07-17"

    undated_with_detail = retrieval.render_row(
        {"claim": "A claim", "detail": "something"}
    )
    assert undated_with_detail == "A claim||+"


def test_a_row_points_at_the_newest_correction(connection):
    """Not the immediate successor: a correction that was itself corrected sends
    the reader one hop short of what is true."""
    store.remember(connection, [{
        "op": "supersede", "supersedes": "eric-moves-in-and-collects-keys",
        "claim": "Eric moved in on the fifth", "type": "action",
        "about_user": True, "window_start": "2026-08-05",
    }])
    store.remember(connection, [{
        "op": "supersede", "supersedes": "eric-moved-in-on-the-fifth",
        "claim": "Eric moved in a day late", "type": "action",
        "about_user": True, "window_start": "2026-08-06",
    }])

    hits = retrieval.search(connection, "moves in collects keys", limit=5,
                            include_superseded=True)
    original = next(h for h in hits if h["id"] == "eric-moves-in-and-collects-keys")

    assert retrieval.render_row(original).endswith("-> Eric moved in a day late")


def test_rows_are_never_truncated(connection):
    """v0 clipped a 684-character summary at 400 and had to instruct the agent to
    dig whenever a row 'looked like it mattered'. A claim is complete by
    construction, so dig stops being repair work for the renderer."""
    hits = retrieval.search(connection, "apartment Leslie")
    assert "…" not in retrieval.render_context(hits)


def test_detail_is_marked_but_not_rendered(connection):
    """An 8-word claim reads as the whole node. Without the marker an agent would
    act on the summary of a node whose entire value sits in unread detail."""
    hits = retrieval.search(connection, "apartment Leslie", limit=1)
    row = retrieval.render_context(hits).splitlines()[1]

    assert row.endswith("+")
    assert "Traver Heights" not in row
    assert "Traver Heights" in retrieval.render_dig(
        retrieval.dig(connection, "Eric's apartment is 2442 Leslie Circle")
    )


def test_claimless_nodes_carry_no_marker(connection):
    node = retrieval.dig(connection, "Eric moves in and collects keys")
    assert node["detail"] is None
    assert not retrieval.render_row(node).endswith("+")


def test_context_carries_no_standing_instructions(connection):
    """Guidance is a per-message tax when it is injected per message; the rules
    for reading these rows live once in meta.md."""
    block = retrieval.render_context(
        retrieval.search(connection, "apartment")
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
    assert old["superseded_by"]["claim"] == "Eric renewed the Leslie Circle lease"

    new = retrieval.dig(connection, "Eric renewed the Leslie Circle lease")
    assert new["stale"] is False
    assert [n["claim"] for n in new["supersedes"]] == [
        "Eric's apartment is 2442 Leslie Circle"
    ]


def test_dig_renders_every_reference_as_a_row(connection):
    """A bare claim says nothing about whether following it is worth a dig."""
    store.remember(connection, [
        {"op": "supersede", "supersedes": "eric-s-apartment-is-2442-leslie-circle",
         "claim": "Eric renewed the Leslie Circle lease", "type": "fact",
         "about_user": True, "window_start": "2027-08-01"},
    ])
    block = retrieval.render_dig(
        retrieval.dig(connection, "Eric renewed the Leslie Circle lease")
    )

    # The superseded node carries detail, so its row has to say so.
    assert "supersedes: Eric's apartment is 2442 Leslie Circle|07-17..27-07-31|+" in block


def test_dig_shortens_its_own_time_field(connection):
    """One date rule everywhere, including the field dig prints for itself."""
    block = retrieval.render_dig(
        retrieval.dig(connection, "Eric moves in and collects keys")
    )
    assert "time: 08-05" in block
    assert "2026-08-05" not in block


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
    assert "time: 08-05" in block


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


def _session_batch(connection, session, count):
    store.remember(connection, [
        {"claim": f"Session note number {index}", "type": "idea",
         "source_session": session, "window_start": "2026-07-26"}
        for index in range(count)
    ])


def test_dig_shows_the_session_s_other_nodes_in_order(connection):
    """A node is one of a handful a session wrote; the others are its context,
    and retrieval can only reach the one that matched the query."""
    _session_batch(connection, "session-abc", 10)

    block = retrieval.render_dig(retrieval.dig(connection, "Session note number 6"))

    assert "session wrote 10 nodes; this is #7:" in block
    assert "->Session note number 6" in block
    # Three each side, in the order the session wrote them, ellipsis for the rest.
    for index in (3, 4, 5, 7, 8, 9):
        assert f"  Session note number {index}" in block
    assert "Session note number 2" not in block
    assert block.count("  ...") == 1        # only the head is truncated

    # Sliced from the heading: the claim also appears in the dig's own title.
    listing = block[block.index("session wrote"):]
    assert listing.index("number 5") < listing.index("number 6") < listing.index("number 7")


def test_the_session_block_is_absent_when_there_are_no_siblings(connection):
    """One node alone in its session has no neighbourhood; the `session:` line
    already says which session it was."""
    _session_batch(connection, "session-solo", 1)

    block = retrieval.render_dig(retrieval.dig(connection, "Session note number 0"))

    assert "session: session-solo" in block
    assert "session wrote" not in block


def test_a_node_at_the_end_truncates_only_the_head(connection):
    _session_batch(connection, "session-tail", 6)

    block = retrieval.render_dig(retrieval.dig(connection, "Session note number 5"))

    assert "session wrote 6 nodes; this is #6:" in block
    assert block.rstrip().splitlines()[-1].startswith("->Session note number 5")


def test_dig_misses_report_the_handle(connection):
    assert retrieval.dig(connection, "No such memory") is None
    assert "No such memory" in retrieval.render_dig(None, "No such memory")
