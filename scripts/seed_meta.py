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
This block is Eric's persistent memory, autoloaded at session start from the \
claude-memory store (D:\\claude-memory). It survives compaction and crosses \
sessions and projects, so treat it as established context rather than something \
Eric said just now. It replaces the former global CLAUDE.md, which no longer \
exists — do not write memory to that file.

READING. What is above is only the always-on set; far more is stored and \
searchable. If a question touches Eric's plans, history, notes, or wiki, search \
the store rather than assuming the answer is absent or asking him to repeat \
himself. Before writing code, search the code-pref nodes — they are deliberately \
not autoloaded, and they cover Eric's conventions on naming, indentation, \
typechecking, and training runs. Hits typed `raw` are unread document text, \
chunked and indexed but never judged; if one actually answers something, extract \
the claim as a typed node and supersede the chunk with it.

WRITING. Be an active memorizer, not a passive one: capture as a normal part of \
finishing a task, without being asked, and at the moment it happens rather than \
batched to the end of a session that may run out of context first. Capture when \
Eric corrects you or pushes back — the highest-value signal, and save the reason \
along with the correction; when he states a preference, constraint, or \
convention; when he reveals a change in his situation; when a decision is made \
after weighing options, together with why it won, so it is not relitigated; and \
when you discover something a future session would waste time rediscovering — a \
build or test command, an environment quirk, a root cause, an API gotcha. Do not \
save what is already plain from the code, git history, or existing docs. When \
unsure, lean toward saving: a slightly redundant node is cheap, a lost insight \
costs a session. After writing one unprompted, say so in one short line so Eric \
can correct it.

Summaries are immutable. When reality changes, supersede the old node rather \
than rewriting it; mark an intention stale when Eric says he no longer intends \
it.\
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
