"""Deterministic markdown ingestion.

Wiki pages are split on headings rather than passed through an LLM: it is free,
lossless, and the headings are already the author's own curation. Re-deriving
that structure with a model would discard real signal and risk inventing claims
the page never made.

Sections land as `type='raw'`: indexed so they can surface, but not claimed to
be true, about anything in particular, or worth keeping. When retrieval pulls a
raw chunk up and an agent actually reads it, the chunk gets superseded by the
typed claims it turned out to contain. Refinement is paid for by attention, and
only where attention was already spent.

Nodes produced here are `origin='derived'` — regenerable, and the only nodes
re-ingest is allowed to replace.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from . import store
from .models import slugify

MAX_SECTION_CHARS = 1500          # keeps each chunk inside the embedder's window
MIN_SECTION_CHARS = 40            # below this a section carries no retrievable signal

HEADING_PATTERN = re.compile(r"^(#{1,6})\s+(.*?)\s*#*$")
FRONTMATTER_PATTERN = re.compile(r"\A---\n.*?\n---\n", re.DOTALL)
WIKILINK_PATTERN = re.compile(r"\[\[([^\]|#]+)")
CODE_FENCE_PATTERN = re.compile(r"^```")


@dataclass
class Section:
    heading_path: str
    body: str
    anchor: str

    @property
    def title(self) -> str:
        return self.heading_path


def _strip_frontmatter(text: str) -> str:
    return FRONTMATTER_PATTERN.sub("", text, count=1)


def _split_long(body: str, max_chars: int) -> list[str]:
    """Split an oversized section on paragraph boundaries."""
    if len(body) <= max_chars:
        return [body]

    chunks: list[str] = []
    current: list[str] = []
    length = 0
    for paragraph in body.split("\n\n"):
        if length + len(paragraph) > max_chars and current:
            chunks.append("\n\n".join(current))
            current, length = [], 0
        current.append(paragraph)
        length += len(paragraph) + 2
    if current:
        chunks.append("\n\n".join(current))
    return chunks


def split_markdown(text: str, max_chars: int = MAX_SECTION_CHARS) -> list[Section]:
    """Split on headings, tracking the heading stack for context."""
    text = _strip_frontmatter(text)
    stack: list[str] = []
    buffer: list[str] = []
    sections: list[Section] = []
    in_code_fence = False

    def flush() -> None:
        body = "\n".join(buffer).strip()
        buffer.clear()
        if len(body) < MIN_SECTION_CHARS:
            return
        heading_path = " > ".join(stack) if stack else "(intro)"
        for index, chunk in enumerate(_split_long(body, max_chars)):
            suffix = f" [{index + 1}]" if index else ""
            sections.append(
                Section(
                    heading_path=heading_path + suffix,
                    body=chunk,
                    anchor=slugify(stack[-1]) if stack else "",
                )
            )

    for line in text.splitlines():
        if CODE_FENCE_PATTERN.match(line):
            in_code_fence = not in_code_fence

        heading = None if in_code_fence else HEADING_PATTERN.match(line)
        if heading:
            flush()
            level = len(heading.group(1))
            del stack[level - 1:]
            stack.append(heading.group(2).strip())
        else:
            buffer.append(line)

    flush()
    return sections


def _file_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _markdown_files(target: Path) -> Iterator[Path]:
    if target.is_file():
        yield target
        return
    for path in sorted(target.rglob("*.md")):
        # Obsidian sync artefacts and hidden folders carry no durable content.
        if any(part.startswith(".") for part in path.parts):
            continue
        yield path


def _clear_derived(
    connection: sqlite3.Connection, source: str
) -> tuple[int, dict[str, str]]:
    """Drop a file's derived nodes, returning the refinements they carried.

    Section ids are slugs of page + heading path, so an edited file re-emits the
    same id for any section whose heading survived. Returning the supersession
    pointers lets the caller re-apply them, or a re-ingest would resurrect chunks
    an agent had already refined and quietly undo that work.
    """
    rows = connection.execute(
        "SELECT id, rowid, superseded_by FROM nodes "
        "WHERE derived_from = ? AND origin = 'derived'",
        (source,),
    ).fetchall()
    refinements = {
        row["id"]: row["superseded_by"] for row in rows if row["superseded_by"]
    }
    for row in rows:
        connection.execute("DELETE FROM nodes_fts WHERE rowid = ?", (row["rowid"],))
        connection.execute("DELETE FROM nodes_vec WHERE rowid = ?", (row["rowid"],))
        connection.execute(
            "DELETE FROM node_edges WHERE src_id = ? OR dst_id = ?",
            (row["id"], row["id"]),
        )
        connection.execute("DELETE FROM nodes WHERE id = ?", (row["id"],))
    return len(rows), refinements


def ingest_path(
    connection: sqlite3.Connection,
    target: Path | str,
    scope: str = "global",
    dry_run: bool = False,
    capture_run_id: str | None = None,
) -> dict[str, Any]:
    """Ingest a markdown file or folder. Unchanged files are skipped."""
    target = Path(target)
    if not target.exists():
        raise FileNotFoundError(target)

    capture_run_id = capture_run_id or store.new_run_id()
    stats = {
        "files_seen": 0,
        "files_ingested": 0,
        "files_skipped": 0,
        "nodes_written": 0,
        "nodes_replaced": 0,
        # A refinement is lost when the heading it hung off was renamed or
        # deleted, so the section no longer re-emits its id. The refined node
        # survives regardless; only the link back to its source is broken.
        "refinements_kept": 0,
        "refinements_lost": 0,
        "capture_run_id": capture_run_id,
    }

    for path in _markdown_files(target):
        stats["files_seen"] += 1
        source = str(path)
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue

        digest = _file_hash(text)
        existing = connection.execute(
            "SELECT content_hash FROM nodes WHERE derived_from = ? LIMIT 1", (source,)
        ).fetchone()
        if existing and existing["content_hash"] == digest:
            stats["files_skipped"] += 1
            continue

        sections = split_markdown(text)
        if not sections:
            stats["files_skipped"] += 1
            continue

        if dry_run:
            stats["files_ingested"] += 1
            stats["nodes_written"] += len(sections)
            continue

        replaced, refinements = _clear_derived(connection, source)
        stats["nodes_replaced"] += replaced

        page = path.stem
        specs = []
        for section in sections:
            wikilinks = WIKILINK_PATTERN.findall(section.body)
            specs.append({
                "id": slugify(f"{page}-{section.heading_path}", max_length=90),
                "title": f"{page} > {section.title}",
                "summary": section.body,
                "type": "raw",
                "scope": scope,
                "origin": "derived",
                "derived_from": source,
                "content_hash": digest,
                "locator": f"{source}#{section.anchor}",
                "keywords": " ".join([page, *wikilinks]),
            })

        store.remember(connection, specs, capture_run_id)

        for raw_id, refined_id in refinements.items():
            restored = connection.execute(
                "UPDATE nodes SET superseded_by = ? WHERE id = ? AND type = 'raw'",
                (refined_id, raw_id),
            ).rowcount
            stats["refinements_kept"] += restored
            stats["refinements_lost"] += 1 - restored
        connection.commit()

        stats["files_ingested"] += 1
        stats["nodes_written"] += len(specs)

    return stats
