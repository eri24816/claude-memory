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
You have a persistent memory system. Memories lives in nodes, created by you across sessions.

READING. There are plenty of memory nodes searchable. Whenever you think context is missing, search before asking Eric for clarification. Before writing code, search the code-pref nodes. Nodes typed `raw` are unread document text, chunked and indexed but never judged; if one actually answers something, extract the claim as a typed node and supersede the chunk with it.

WRITING. Be an active memorizer, not a passive one: capture as a normal part of finishing a task, without being asked, and at the moment it happens rather than batched to the end of a session that may run out of context first. Capture when Eric corrects you or pushes back — the highest-value signal, and save the reason along with the correction; when he states a preference, constraint, or convention; when he reveals a change in his situation; when a decision is made after weighing options, together with why it won, so it is not relitigated; and when you discover something a future session would waste time rediscovering — a build or test command, an environment quirk, a root cause, an API gotcha. Do not save what is already plain from the code, git history, or existing docs. When unsure, lean toward saving: a slightly redundant node is cheap, a lost insight costs a session. After writing one unprompted, say so in one short line so Eric can correct it.

To be specific, these are node types you can write and read:
| Type | Meaning | Time |
|---|---|---|
| `fact` | A proposition true **at least since** `window_start` | `[window_start, window_end]`; null end = unknown |
| `action` | Someone did something — user, agent, or third party | `window_start` (= end, or a range if durative) |
| `todo` | Something necessary or decided — a todo list or calendar item | `window_end` = due date, if any |
| `intention` | Someone wants to and may do something — not yet committed | when expressed |
| `idea` | A thought or proposal, large or small | when thought |
| `conv-pref` | How the agent should communicate or behave | — |
| `code-pref` | A coding constraint or convention | — |

Summaries are immutable. When reality changes, supersede the old node rather than rewriting it; mark an intention stale when Eric says he no longer intends it.

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
