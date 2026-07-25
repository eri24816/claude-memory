"""Ingestion and the per-query raw cap.

The cap is the whole reason `raw` came back in 0.2.0, so it is tested against
the corpus shape that broke v0: many near-identical chunks of one topic against
a handful of typed nodes.
"""

from __future__ import annotations

import pytest

from claude_memory import db, ingest, models, retrieval, store

PAGE = """---
tags: [networking]
---

# Transport Layer Security

TLS secures traffic between a client and a server, and every section below
concerns some part of how that handshake is negotiated in practice.

## Session resumption

Session resumption lets a client skip the full handshake by presenting a ticket
the server issued earlier, which saves a round trip on reconnect.

## Certificate pinning

Certificate pinning ties a host to a known key so a mis-issued certificate from
any other authority is rejected by the client outright.

### Failure modes

Pinning a certificate rather than a public key breaks on every routine renewal,
which is the usual way a pinned deployment takes itself offline.
"""

# A second document, because MAX_PER_SOURCE already holds one page to two hits.
# The per-query cap is what bounds the *corpus*, so a single-page fixture cannot
# tell the two limits apart and would pass with the cap deleted.
SECOND_PAGE = """# Handshake tracing

Capturing a TLS handshake with a key log file lets a debugger decrypt the
session afterwards, which is how a certificate problem gets diagnosed at all.

## Ticket lifetime

A session ticket that outlives its key material fails resumption silently and
falls back to a full handshake, which shows up only as latency.
"""

TYPED = [
    {
        "claim": "Eric's apartment is 2442 Leslie Circle",
        "type": "fact", "about_user": True, "window_start": "2026-07-17",
    },
    {
        "claim": "Session tickets rotate hourly in production",
        "type": "fact", "about_user": False, "window_start": "2026-07-01",
    },
]


@pytest.fixture
def wiki(tmp_path):
    path = tmp_path / "wiki"
    path.mkdir()
    (path / "tls.md").write_text(PAGE, encoding="utf-8")
    (path / "tracing.md").write_text(SECOND_PAGE, encoding="utf-8")
    return path


@pytest.fixture
def connection():
    conn = db.connect(":memory:")
    yield conn
    conn.close()


def test_sections_land_as_claim_and_detail(connection, wiki):
    """The heading is the claim and the body is the detail, not the reverse."""
    ingest.ingest_path(connection, wiki)

    rows = connection.execute(
        "SELECT claim, detail, type, about_user FROM nodes ORDER BY claim"
    ).fetchall()
    assert rows, "ingest wrote nothing"
    for row in rows:
        assert row["type"] == "raw"
        assert row["about_user"] is None
        assert row["claim"].split(" > ")[0] in {"tls", "tracing"}
        assert row["detail"] and len(row["detail"]) > len(row["claim"])


def test_raw_claim_keeps_the_page_and_the_deepest_heading():
    """An over-long path drops middle headings; the tail is what disambiguates."""
    claim = ingest._raw_claim("networking", " > ".join([
        "Transport Layer Security",
        "Interoperability with older middleboxes",
        "Session resumption",
    ]))

    assert len(claim) <= models.RAW_CLAIM_MAX_CHARS
    assert claim.startswith("networking > ")
    assert claim.endswith("Session resumption")


def test_raw_is_exempt_from_the_word_cap_but_not_the_character_cap():
    long_heading = models.Node(
        id="x", claim="wiki > " + " ".join(["word"] * 12), type="raw",
        detail="body",
    )
    models.validate(long_heading)          # twelve words, and that is fine

    too_long = models.Node(
        id="y", claim="wiki > " + "x" * models.RAW_CLAIM_MAX_CHARS, type="raw",
        detail="body",
    )
    with pytest.raises(models.InvariantError, match="characters"):
        models.validate(too_long)


def test_raw_without_detail_is_refused():
    """The claim is only a heading, so a detail-less raw node holds nothing."""
    with pytest.raises(models.InvariantError, match="raw requires detail"):
        models.validate(models.Node(id="z", claim="wiki > Heading", type="raw"))


def test_raw_hits_are_capped_per_query(connection, wiki):
    """The v0 failure: a matching page sweeps the result set.

    Every chunk of the page is about TLS, so an unbounded search returns chunks
    and nothing else. The cap is what keeps the typed node in the list.
    """
    ingest.ingest_path(connection, wiki)
    store.remember(connection, TYPED)

    query = "TLS session resumption handshake"
    hits = retrieval.search(connection, query, limit=10)
    raw_hits = [hit for hit in hits if hit["type"] == "raw"]
    uncapped = [hit for hit in retrieval.search(connection, query, limit=10,
                                                raw_limit=99)
                if hit["type"] == "raw"]

    # Literal 2, not RAW_PER_QUERY: an assertion written against the constant
    # moves with it, so raising the cap to 999 would leave this test green while
    # the flood it exists to prevent came back.
    assert len(raw_hits) <= 2
    assert len(uncapped) > 2, "fixture cannot flood, so the cap is untested here"
    assert any(hit["type"] != "raw" for hit in hits), "raw swamped the result set"


def test_the_cap_bounds_raw_without_demoting_it(connection, wiki):
    """A capped-away chunk must not cost a surviving chunk its rank.

    The v0 penalty made every raw hit weaker; the cap makes the third one absent
    and leaves the first two exactly where they were earned.
    """
    ingest.ingest_path(connection, wiki)
    store.remember(connection, TYPED)

    uncapped = retrieval.search(connection, "certificate pinning renewal",
                                limit=10, raw_limit=99)
    capped = retrieval.search(connection, "certificate pinning renewal", limit=10)

    surviving = [hit["id"] for hit in capped if hit["type"] == "raw"]
    expected = [hit["id"] for hit in uncapped if hit["type"] == "raw"]
    assert len(expected) > len(surviving), "nothing was capped, so nothing is proven"
    assert surviving == expected[: len(surviving)]

    scores = {hit["id"]: hit["score"] for hit in uncapped}
    for hit in capped:
        if hit["type"] == "raw":
            assert hit["score"] == scores[hit["id"]]


def test_asking_for_raw_explicitly_opts_out_of_the_cap(connection, wiki):
    """`--type raw` is not a result set being swamped; it is one being answered."""
    ingest.ingest_path(connection, wiki)

    hits = retrieval.search(connection, "TLS handshake certificate session",
                            limit=10, node_type="raw")
    assert len(hits) > 2


def test_the_cap_does_not_shrink_what_the_caller_receives(connection, wiki):
    """Filtered before LIMIT: capped chunks must not consume result slots."""
    ingest.ingest_path(connection, wiki)
    store.remember(connection, TYPED)

    hits = retrieval.search(connection, "TLS session ticket apartment", limit=4)
    assert len(hits) == 4


def test_raw_never_reaches_the_autoload_set(connection, wiki):
    """T1 is personal-only, and a chunk has no about_user to be personal with."""
    ingest.ingest_path(connection, wiki)
    store.remember(connection, TYPED)

    selected = retrieval.assemble_t1(connection)
    assert selected, "the typed fact should still autoload"
    assert not any(node["type"] == "raw" for node in selected)


def test_reingest_is_a_no_op_when_the_file_is_unchanged(connection, wiki):
    ingest.ingest_path(connection, wiki)
    again = ingest.ingest_path(connection, wiki)

    assert again["files_skipped"] == again["files_seen"] == 2
    assert again["nodes_written"] == 0


def test_reingest_preserves_a_refinement(connection, wiki):
    """An edited page must not resurrect a chunk an agent already refined."""
    ingest.ingest_path(connection, wiki)
    raw_id = connection.execute(
        "SELECT id FROM nodes WHERE claim LIKE '%Session resumption%'"
    ).fetchone()["id"]

    store.remember(connection, [{
        "claim": "Session resumption skips the full handshake",
        "type": "fact", "about_user": False, "window_start": "2026-07-25",
    }])
    refined_id = "session-resumption-skips-the-full-handshake"
    connection.execute(
        "UPDATE nodes SET superseded_by = ? WHERE id = ?", (refined_id, raw_id)
    )
    connection.commit()

    (wiki / "tls.md").write_text(PAGE + "\n## Postscript\n\n" + "Added later, "
                                * 5 + "\n", encoding="utf-8")
    stats = ingest.ingest_path(connection, wiki)

    assert stats["refinements_kept"] == 1
    survivor = connection.execute(
        "SELECT superseded_by FROM nodes WHERE id = ?", (raw_id,)
    ).fetchone()
    assert survivor["superseded_by"] == refined_id
