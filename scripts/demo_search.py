"""Exercise hybrid retrieval against the dev store."""

from claude_memory import connect, search

QUERIES = [
    "where does eric live",          # semantic: no shared terms with the node
    "WinError 1455",                 # lexical: exact rare token, BM25 territory
    "roommate names",                # semantic
    "credit card for students",      # should surface the about_user=false node
]

connection = connect("dev.db")
for query in QUERIES:
    print(f"\n=== {query} ===")
    for hit in search(connection, query, limit=3):
        print(f"  {hit['score']:.4f}  [{hit['type']}] {hit['summary'][:76]}")
connection.close()
