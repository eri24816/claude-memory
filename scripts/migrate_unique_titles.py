"""Make titles unique, so they can serve as the agent-facing handle.

Retrieval prints a title per line and an agent digs by that title, which only
works if a title resolves to exactly one node. Superseding created the one
existing collision: a replacement node kept its predecessor's title.

The live node keeps the bare title; superseded ones are suffixed in age order. A
title names a concept rather than a version, so it has to resolve to the node
that is currently true — superseded nodes are excluded from retrieval anyway and
are only reached by deliberately digging through history.
"""

import sqlite3
import sys

from claude_memory import db

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# Raw connection, not db.connect(): connect() applies schema.sql, which now
# declares the unique index this script exists to make satisfiable.
connection = sqlite3.connect(db.DEFAULT_DB_PATH)
connection.row_factory = sqlite3.Row

collisions = connection.execute(
    "SELECT title FROM nodes WHERE title IS NOT NULL "
    "GROUP BY title HAVING COUNT(*) > 1"
).fetchall()

for row in collisions:
    # Current node first, then the superseded ones oldest-first. Ordering on
    # `updated` would be wrong: superseding bumps the predecessor's timestamp,
    # so the dead node looks newer than the one that replaced it.
    duplicates = connection.execute(
        "SELECT id, title FROM nodes WHERE title = ? "
        "ORDER BY superseded_by IS NOT NULL, rowid",
        (row["title"],),
    ).fetchall()
    for position, node in enumerate(duplicates[1:], start=2):
        renamed = f"{row['title']} ({position})"
        connection.execute(
            "UPDATE nodes SET title = ? WHERE id = ?", (renamed, node["id"])
        )
        print(f"  renamed {node['id']} -> {renamed!r}")

untitled = connection.execute(
    "SELECT COUNT(*) FROM nodes WHERE title IS NULL OR title = ''"
).fetchone()[0]
if untitled:
    raise SystemExit(f"{untitled} nodes have no title; titles are now required")

connection.execute("CREATE UNIQUE INDEX IF NOT EXISTS nodes_title ON nodes(title)")
connection.commit()
print(f"{len(collisions)} collisions resolved; unique index created")
connection.close()
