"""Node model, taxonomy, and the invariants the store enforces."""

from __future__ import annotations

import datetime as dt
import re
import unicodedata
from dataclasses import dataclass, field

# v1: only the world-describing types remain here. conv-pref, code-pref and
# meta moved to files (see settings.py) because they are rules, which have a
# current form rather than a history. `raw` is gone until 0.2.0 reintroduces it
# with a per-query cap instead of a score penalty.
NODE_TYPES = frozenset({
    "fact",
    "action",
    "todo",
    "intention",
    "idea",
})

# Named so the error can say where they went. A type that silently fails
# validation would send the agent looking for a typo instead of a file.
RETIRED_TYPES = {
    "conv-pref": "settings/conv.md",
    "code-pref": "settings/code.md",
    "meta": "settings/meta.md",
    "raw": "removed in 0.1.0; returns in 0.2.0",
}

# A word budget, not a character budget. Characters produce truncation-shaped
# prose; words force the function words out, which is what makes a claim read
# as "Eric will apply for Discovery card" rather than a clipped sentence.
CLAIM_MAX_WORDS = 8

# Types that sit on the commitment ladder or describe the world, and therefore
# must declare whether they fall inside the user's personal sphere.
# 'raw' is excluded on purpose: nothing has read the chunk closely enough to
# answer the question, and guessing False would be a claim the ingest never made.
TYPES_REQUIRING_ABOUT_USER = frozenset({"fact", "action", "todo", "intention"})

# Superseding these would hide them from retrieval, which filters on
# superseded_by IS NULL. An idea stays valid however much work it spawns.
TYPES_NEVER_SUPERSEDED = frozenset({"idea"})

EDGE_RELATIONS = frozenset({"motivates", "relates"})

ORIGINS = frozenset({"original", "derived"})

DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class InvariantError(ValueError):
    """Raised when a write would leave the graph in an invalid state."""


class AmbiguousHandle(InvariantError):
    """A handle matched several nodes.

    Distinct from "no such node" because the two demand opposite responses: a
    miss means the memory does not exist yet and may be worth writing, while an
    ambiguity means it exists more than once and writing again would add a
    duplicate the store can never merge. Callers that flatten both into None
    reliably choose wrong.
    """


@dataclass
class Node:
    id: str
    claim: str
    type: str
    detail: str | None = None
    about_user: bool | None = None
    scope: str = "global"
    window_start: str | None = None
    window_end: str | None = None
    stale: bool = False
    superseded_by: str | None = None
    origin: str = "original"
    parent: str | None = None
    derived_from: str | None = None
    content_hash: str | None = None
    locator: str | None = None
    source_session: str | None = None
    updated: str | None = None
    keywords: str | None = None
    edges: list[dict[str, str]] = field(default_factory=list)

    def embedding_text(self) -> str:
        """Focused text for the dense index: metadata dilutes the vector.

        Includes the detail. Only the claim is ever *rendered*, but indexing the
        claim alone would mean a node could never be found by anything its
        detail says -- compression is a display decision and must not reach the
        index.
        """
        if self.detail:
            return f"{self.claim}. {self.detail}"
        return self.claim

    def lexical_keywords(self) -> str:
        """Superset text for BM25: extra tokens only help lexical matching."""
        parts = [self.type, self.scope]
        if self.keywords:
            parts.append(self.keywords)
        return " ".join(part for part in parts if part)


def slugify(text: str, max_length: int = 60) -> str:
    normalised = unicodedata.normalize("NFKD", text)
    ascii_text = normalised.encode("ascii", "ignore").decode("ascii").lower()
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_text).strip("-")
    return slug[:max_length] or "node"


def validate(node: Node) -> None:
    """Raise InvariantError if the node violates a design invariant."""
    errors: list[str] = []

    if not node.id:
        errors.append("id is required")
    if not node.claim or not node.claim.strip():
        errors.append("claim is required")
    else:
        words = node.claim.split()
        if len(words) > CLAIM_MAX_WORDS:
            errors.append(
                f"claim is {len(words)} words, max {CLAIM_MAX_WORDS}: "
                f"{node.claim!r}. Compress it and move the rest to detail -- do "
                "not include the date, the window renders itself"
            )
    if node.detail is not None and not node.detail.strip():
        # An empty string is almost always an accident; NULL is the honest way
        # to say a node is claim-only, and it is the common case.
        errors.append("detail must be non-empty text or null, not an empty string")

    if node.type in RETIRED_TYPES:
        errors.append(
            f"type {node.type!r} is no longer stored as a node -- "
            f"it lives in {RETIRED_TYPES[node.type]}"
        )
    elif node.type not in NODE_TYPES:
        errors.append(f"unknown type {node.type!r}; expected one of {sorted(NODE_TYPES)}")
    if node.origin not in ORIGINS:
        errors.append(f"unknown origin {node.origin!r}")

    if node.type in TYPES_REQUIRING_ABOUT_USER:
        if node.about_user is None:
            errors.append(f"about_user is required for type {node.type!r}")
    elif node.about_user is not None:
        errors.append(f"about_user must be null for type {node.type!r}")

    for label, value in (("window_start", node.window_start), ("window_end", node.window_end)):
        if value is not None and not DATE_PATTERN.match(value):
            errors.append(f"{label} must be an ISO date (YYYY-MM-DD), got {value!r}")

    if node.window_start and node.window_end and node.window_start > node.window_end:
        errors.append("window_start must not be after window_end")

    if node.superseded_by == node.id:
        errors.append("a node cannot supersede itself")

    for edge in node.edges:
        relation = edge.get("rel")
        if relation not in EDGE_RELATIONS:
            errors.append(f"unknown edge relation {relation!r}")
        if not edge.get("dst"):
            errors.append("edge is missing dst")
        if edge.get("dst") == node.id:
            errors.append("a node cannot link to itself")

    if errors:
        raise InvariantError(f"invalid node {node.id!r}: " + "; ".join(errors))


def today() -> str:
    return dt.date.today().isoformat()


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
