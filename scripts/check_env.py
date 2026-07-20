"""Verify the SQLite feature set this project depends on."""

import sqlite3

import sqlite_vec

connection = sqlite3.connect(":memory:")
connection.enable_load_extension(True)
sqlite_vec.load(connection)
connection.enable_load_extension(False)

vec_version = connection.execute("SELECT vec_version()").fetchone()[0]
print("sqlite_vec:", vec_version)

compile_options = [row[0] for row in connection.execute("PRAGMA compile_options")]
print("fts5:", any("FTS5" in option for option in compile_options))

connection.execute("CREATE VIRTUAL TABLE t USING fts5(body)")
connection.execute("INSERT INTO t(rowid, body) VALUES (1, 'hello world')")
score = connection.execute(
    "SELECT bm25(t) FROM t WHERE t MATCH 'hello'"
).fetchone()[0]
print("bm25 score:", score)

connection.execute("CREATE VIRTUAL TABLE v USING vec0(embedding float[3])")
connection.execute(
    "INSERT INTO v(rowid, embedding) VALUES (1, ?)",
    (sqlite_vec.serialize_float32([0.1, 0.2, 0.3]),),
)
neighbour = connection.execute(
    "SELECT rowid, distance FROM v WHERE embedding MATCH ? AND k = 1",
    (sqlite_vec.serialize_float32([0.1, 0.2, 0.3]),),
).fetchone()
print("knn:", neighbour)
