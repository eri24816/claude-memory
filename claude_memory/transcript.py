"""Reads Claude Code's own .jsonl transcripts.

A node's locator/source_session point at a message inside one of these files
(see store._live_transcript_uuid for how they get stamped). Writing is
best-effort and never raises; reading here can be stricter, since a caller
asking to see the transcript wants to know when it fails, not silently get
nothing.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Iterator


def projects_dir() -> Path:
    return Path(os.environ.get(
        "CLAUDE_MEMORY_TRANSCRIPTS_DIR", str(Path.home() / ".claude" / "projects")
    ))


def find_transcript(session_id: str) -> Path | None:
    matches = list(projects_dir().glob(f"**/{session_id}.jsonl"))
    return matches[0] if matches else None


def is_authored_message(message: dict) -> bool:
    """True for something a person actually typed, not a tool_result echoed
    back as a user-role turn -- the Anthropic API's convention for feeding
    tool output back into the conversation.
    """
    content = message.get("content")
    if isinstance(content, str):
        return bool(content.strip())
    if isinstance(content, list):
        return any(
            isinstance(block, dict) and block.get("type") == "text"
            for block in content
        )
    return False


def iter_events(path: Path) -> Iterator[dict[str, Any]]:
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            yield event


def extract_text(message: dict) -> str:
    """Best-effort plain text for one message, for display -- not a faithful
    reconstruction of tool calls, just enough to show what was said.
    """
    content = message.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = []
    for block in content:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "text":
            parts.append(block.get("text", ""))
        elif kind == "tool_use":
            parts.append(f"[tool call: {block.get('name', '?')}]")
        elif kind == "tool_result":
            inner = block.get("content")
            if isinstance(inner, list):
                inner = "".join(
                    b.get("text", "") for b in inner
                    if isinstance(b, dict) and b.get("type") == "text"
                )
            snippet = str(inner or "")
            if len(snippet) > 300:
                snippet = snippet[:300] + "..."
            parts.append(f"[tool result: {snippet}]")
    return "\n".join(part for part in parts if part)


def excerpt(session_id: str, locator: str, window: int = 3) -> dict[str, Any] | None:
    """The messages surrounding one uuid in a session's transcript.

    Returns None when the transcript file or the uuid inside it can't be
    found -- a session that already rotated out, or a locator that predates
    this format. `window` messages of context on each side, not just the one
    hit, since the point is reconstructing what was actually being discussed.
    """
    path = find_transcript(session_id)
    if path is None:
        return None

    events = [
        event for event in iter_events(path)
        if isinstance(event.get("message"), dict) and event.get("uuid")
    ]
    index = next(
        (i for i, event in enumerate(events) if event["uuid"] == locator), None
    )
    if index is None:
        return None

    lo, hi = max(0, index - window), min(len(events), index + window + 1)
    return {
        "path": str(path),
        "index": index,
        "messages": [
            {
                "uuid": event["uuid"],
                "role": event["message"].get("role"),
                "text": extract_text(event["message"]),
                "is_hit": i == index,
            }
            for i, event in enumerate(events[lo:hi], start=lo)
        ],
    }
