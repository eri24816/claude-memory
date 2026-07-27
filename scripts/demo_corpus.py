"""Judge retrieval against the real 500-node corpus."""

import sys

from claude_memory import assemble_t1, connect, render_t1, search
from claude_memory.retrieval import session_history

# The vault contains CJK; the Windows console defaults to cp1252.
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

QUERIES = [
    "where am I moving to next month",   # personal — must beat 490 wiki nodes
    "what are my roommates called",      # personal
    "who is Herman Dong",                # wiki
    "symbolic music generation models",  # wiki, conceptual
    "WinError 1455",                     # exact rare token
]

connection = connect()

block = render_t1(assemble_t1(connection), session_history(connection))
print(f"T1: {len(block)} chars, {block.count(chr(10) + '- ')} entries\n")

for query in QUERIES:
    print(f"=== {query} ===")
    for hit in search(connection, query, limit=4):
        lexical = hit["lexical_rank"] or "-"
        semantic = hit["semantic_rank"] or "-"
        label = (hit["title"] or hit["summary"])[:70].replace("\n", " ")
        marker = "*" if hit["about_user"] else " "
        print(f" {marker} {hit['score']:.4f} bm25={lexical:>4} vec={semantic:>4}  {label}")
    print()

connection.close()
