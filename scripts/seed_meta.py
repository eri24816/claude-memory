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
Eric said just now.

Reading: what is above is only the always-on set. Far more is stored and \
searchable, so if a question touches Eric's plans, history, notes, or wiki, \
search the store rather than assuming it is absent or asking him to repeat it. \
Before writing code, read the code-pref nodes; they are not autoloaded.

Hits typed `raw` are unread document text, chunked and indexed but never \
judged. If a raw chunk actually answers something, extract the claim as a typed \
node and supersede the chunk with it.

Writing: maintain this memory actively, without being asked. When Eric corrects \
you, states a preference, reveals a change in his situation, or you learn \
something a future session would waste time rediscovering, capture it. \
Summaries are immutable: when reality changes, supersede the old node instead of \
rewriting it, and mark an intention stale when he says he no longer intends it.\
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
