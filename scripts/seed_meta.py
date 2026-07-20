"""Seed the meta node: the memory system's description of itself.

T1 was autoloading facts with no instructions attached, so an agent saw what was
remembered but nothing about how to read, extend, or correct it. The meta node
is the one node whose whole job is to be read first.

Superseding is deliberate — this text changes as the system does, and the old
wording stays in history rather than being edited away.
"""

import sys

from claude_memory import connect
from claude_memory.store import remember

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SUMMARY = """\
You have a persistent memory of nodes, written by you and shared across every session and project. What is above is only the always-on subset; several hundred more nodes, including Eric's whole wiki, are searchable.

This overrides the file-based memory described in your system prompt. Ignore it: the directory it names is empty and abandoned, and there is no CLAUDE.md any more. Never write memory to a file — everything below is how memory works now.

READING. Whenever you notice context is missing, search before asking Eric to repeat himself. `python -m claude_memory search "<query>"` prints one row per hit as `type, date, title, content`, truncated. Add `--type code-pref` before writing code — a plain query rarely surfaces terse conventions. Titles are unique handles: `python -m claude_memory dig "<title>"` returns a node in full, with its window, staleness, and links; dig whenever a truncated row looks like it matters rather than acting on the fragment. Rows typed `raw` are unread notes, chunked and indexed without anything having judged them — treat them as leads, not as fact.

WRITING. Be an active memorizer, not a passive one: capture as a normal part of finishing a task, without being asked, and at the moment it happens rather than batched to the end of a session that may run out of context first. Capture when Eric corrects you or pushes back — the highest-value signal, and save the reason, not just the correction; when he states a preference, constraint, or convention; when he reveals a change in his situation; when a decision is made after weighing options, together with why it won, so it is not relitigated; and when you discover something a future session would waste time rediscovering — a build or test command, an environment quirk, a root cause, an API gotcha. Do not save what is already plain from the code, git history, or existing docs. When unsure, lean toward saving: a slightly redundant node is cheap, a lost insight costs a session. After writing one unprompted, say so in one short line so Eric can correct it.

Summaries are immutable: never edit a node, always supersede it. Load the `memory` skill for how to write — the field schema, the node types, and how to supersede or retire what is already stored.

\
"""

connection = connect()
existing = connection.execute(
    "SELECT id FROM nodes WHERE type = 'meta' AND superseded_by IS NULL"
).fetchall()

spec = {"id": "memory-meta", "title": "How this memory works",
        "summary": SUMMARY, "type": "meta"}
if existing:
    spec.update(op="supersede", supersedes=existing[0]["id"])

result = remember(connection, [spec])
print(f"{'superseded' if existing else 'inserted'}: {result['written'][0]}")
connection.close()
