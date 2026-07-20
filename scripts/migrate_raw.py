"""Retype already-ingested wiki sections from 'fact' to 'raw'.

The first ingest called every markdown section a fact with about_user=False.
That was a category error: a 1500-character prose chunk asserts nothing in
particular, and claiming it is a fact about the world puts unread text on equal
footing with claims something actually judged.

Only derived nodes are touched, so hand-written facts are untouched. Rewrites
the FTS row too, since node type is part of the lexical keyword text.
"""

import sys

from claude_memory import connect

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

connection = connect()
rows = connection.execute(
    """
    SELECT n.rowid, n.id, f.title, f.summary, f.keywords
    FROM nodes AS n JOIN nodes_fts AS f ON f.rowid = n.rowid
    WHERE n.origin = 'derived' AND n.type = 'fact'
    """
).fetchall()

for row in rows:
    connection.execute(
        "UPDATE nodes SET type = 'raw', about_user = NULL WHERE id = ?", (row["id"],)
    )
    # lexical_keywords() emits "<type> <scope> <extras>", so the type is the
    # leading token; rewriting it in place keeps the rest of the keyword text.
    keywords = row["keywords"] or ""
    if keywords.startswith("fact "):
        keywords = "raw " + keywords[len("fact "):]
    connection.execute("DELETE FROM nodes_fts WHERE rowid = ?", (row["rowid"],))
    connection.execute(
        "INSERT INTO nodes_fts (rowid, title, summary, keywords) VALUES (?, ?, ?, ?)",
        (row["rowid"], row["title"], row["summary"], keywords),
    )

connection.commit()
print(f"retyped {len(rows)} derived nodes to 'raw'")
print(dict(connection.execute(
    "SELECT COUNT(*) AS raw FROM nodes WHERE type = 'raw'"
).fetchone()))
connection.close()
