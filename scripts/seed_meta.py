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

WRITING. Be an active memorizer, not a passive one: capture as a normal part of finishing a task, unasked, at the moment it happens rather than batched for later. Capture corrections and pushback — the highest-value signal, with the reason, not just the fix; stated preferences, constraints, conventions; changes in Eric's situation; and anything you establish through real work that a future session would waste time re-deriving — a build/test command, an environment quirk, an API gotcha, or a fact confirmed or corrected through research — the moment you establish it, whether or not it leads to any decision. Capture a decision itself once one is actually made, with the reason it won, so it isn't relitigated: a decision is not the same as a finding, and a finding does not need a decision to be worth saving. Capture Eric's own request as an action when it triggers real work — research, comparison, verification — separate from whatever that work produces. Do not save what's already plain from the code, git history, or existing docs. Lean toward saving when unsure: redundancy is cheap, a lost insight costs a session. Say so in one line after an unprompted write.

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
