"""Node model, taxonomy, and the invariants the store enforces."""

from __future__ import annotations

import datetime as dt
import re
import unicodedata
from dataclasses import dataclass, field

NODE_TYPES = frozenset({
    "raw",
    "fact",
    "action",
    "todo",
    "intention",
    "idea",
    "meta",
    "conv-pref",
    "code-pref",
})

# Types that sit on the commitment ladder or describe the world, and therefore
# must declare whether they fall inside the user's personal sphere.
# 'raw' is excluded on purpose: nothing has read the chunk closely enough to
# answer the question, and guessing False would be a claim the ingest never made.
TYPES_REQUIRING_ABOUT_USER = frozenset({"fact", "action", "todo", "intention"})

# Unread source text: a section of a document, chunked mechanically and indexed
# so it can surface, not because anything has judged what it says. A raw node is
# a standing invitation to be replaced by the claims it actually contains.
TYPE_RAW = "raw"

# Superseding these would hide them from retrieval, which filters on
# superseded_by IS NULL. An idea stays valid however much work it spawns.
TYPES_NEVER_SUPERSEDED = frozenset({"idea"})

EDGE_RELATIONS = frozenset({"motivates", "relates"})

ORIGINS = frozenset({"original", "derived"})

DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class InvariantError(ValueError):
    """Raised when a write would leave the graph in an invalid state."""


@dataclass
class Node:
    id: str
    summary: str
    type: str
    title: str | None = None
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
        """Focused text for the dense index: metadata dilutes the vector."""
        if self.title:
            return f"{self.title}. {self.summary}"
        return self.summary

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
    if not node.summary or not node.summary.strip():
        errors.append("summary is required")
    if node.type not in NODE_TYPES:
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
