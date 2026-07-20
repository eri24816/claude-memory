"""Compare flat vs stratified retrieval on the real corpus."""

import sys

from claude_memory import connect, search
from claude_memory.retrieval import search_stratified

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

QUERY = "where am I moving to next month"
connection = connect()


def show(label, hits):
    print(f"=== {label} ===")
    for hit in hits:
        marker = "*" if hit["about_user"] else " "
        title = (hit["title"] or hit["summary"])[:64].replace("\n", " ")
        print(f" {marker} {hit['score']:.4f}  {title}")
    print()


show("flat (before)", search(connection, QUERY, limit=5))
show("stratified (after)", search_stratified(connection, QUERY))
connection.close()
