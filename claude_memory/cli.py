"""Agent-facing entry point. There is deliberately no user-facing CLI:
the user talks to the agent, and the agent calls this.

    python -m claude_memory search "michigan housing"
    python -m claude_memory dig "Ann Arbor apartment"
    python -m claude_memory t1 --scope project:polygenie --render
    python -m claude_memory remember --file nodes.json
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

from . import db, retrieval, store
from .models import InvariantError

READ_ONLY_ACTIONS = {
    sqlite3.SQLITE_SELECT,
    sqlite3.SQLITE_READ,
    sqlite3.SQLITE_FUNCTION,
    sqlite3.SQLITE_RECURSIVE,
}


def _emit(payload: object) -> None:
    json.dump(payload, sys.stdout, indent=2, ensure_ascii=False)
    sys.stdout.write("\n")


def _read_specs(inline: str | None, path: str | None) -> list[dict]:
    """Accept a file, an inline string, or stdin.

    Prefer --file on Windows: PowerShell mangles payloads piped to stdin.
    """
    if path:
        # utf-8-sig, not utf-8: PowerShell's `Out-File -Encoding utf8` writes a
        # BOM, and --file is the documented path on Windows precisely because
        # piping is unreliable there. Plain utf-8 rejects the BOM outright, so
        # the natural way to produce the file would break every write.
        raw = Path(path).read_text(encoding="utf-8-sig")
    elif inline:
        raw = inline
    else:
        raw = sys.stdin.read()
    specs = json.loads(raw)
    return specs if isinstance(specs, list) else [specs]


def _deny_writes(action, *_args):
    """SQLite authorizer: the sql escape hatch is read-only by construction."""
    return sqlite3.SQLITE_OK if action in READ_ONLY_ACTIONS else sqlite3.SQLITE_DENY


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="claude_memory")
    parser.add_argument("--db", default=None, help="path to the SQLite store")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("init", help="create the store and apply the schema")

    remember_parser = subparsers.add_parser("remember", help="insert or supersede nodes")
    remember_parser.add_argument("--file", default=None, help="path to a JSON array")
    remember_parser.add_argument("--json", default=None, help="inline JSON array")
    remember_parser.add_argument("--run-id", default=None)

    search_parser = subparsers.add_parser("search", help="hybrid BM25 + vector retrieval")
    search_parser.add_argument("query")
    search_parser.add_argument("--scope", default=None)
    search_parser.add_argument("--limit", type=int, default=10)
    search_parser.add_argument("--parent", default=None, help="dig down into an abstraction")
    search_parser.add_argument("--type", default=None, dest="node_type",
                               help="restrict to one node type, e.g. code-pref")
    search_parser.add_argument("--json", action="store_true",
                               help="raw rows with ranks, instead of the rendered block")
    search_parser.add_argument("--include-autoloaded", action="store_true",
                               help="do not filter out what T1 already loaded")

    dig_parser = subparsers.add_parser("dig", help="expand one node by its title")
    dig_parser.add_argument("title")
    dig_parser.add_argument("--json", action="store_true")

    t1_parser = subparsers.add_parser("t1", help="assemble the autoload set")
    t1_parser.add_argument("--scope", default=None)
    t1_parser.add_argument("--render", action="store_true", help="markdown, not JSON")

    stale_parser = subparsers.add_parser("stale", help="mark a node no longer true")
    stale_parser.add_argument("title", help="node title (an id also works)")

    rollback_parser = subparsers.add_parser("rollback", help="undo a capture run")
    rollback_parser.add_argument("capture_run_id")

    ingest_parser = subparsers.add_parser("ingest", help="heading-split a file or folder")
    ingest_parser.add_argument("path")
    ingest_parser.add_argument("--scope", default="global")
    ingest_parser.add_argument("--dry-run", action="store_true")

    sql_parser = subparsers.add_parser("sql", help="read-only query escape hatch")
    sql_parser.add_argument("query")

    snapshot_parser = subparsers.add_parser("snapshot", help="consistent backup")
    snapshot_parser.add_argument("destination")

    args = parser.parse_args(argv)
    connection = db.connect(args.db)

    try:
        if args.command == "init":
            _emit({"db": str(Path(args.db) if args.db else db.DEFAULT_DB_PATH)})

        elif args.command == "remember":
            specs = _read_specs(args.json, args.file)
            _emit(store.remember(connection, specs, args.run_id))

        elif args.command == "search":
            # The rendered block is the default: it is what an agent should read,
            # and the ranks only matter when debugging retrieval itself.
            hits = retrieval.search(
                connection, args.query, limit=args.limit,
                scope=args.scope, parent=args.parent, node_type=args.node_type,
                exclude_ids=None if args.include_autoloaded
                else retrieval.t1_ids(connection, scope=args.scope),
            )
            if args.json:
                _emit(hits)
            else:
                sys.stdout.write(retrieval.render_context(hits) + "\n")

        elif args.command == "dig":
            node = retrieval.dig(connection, args.title)
            if args.json:
                _emit(node)
            else:
                sys.stdout.write(retrieval.render_dig(node, args.title) + "\n")
            if node is None:
                return 4

        elif args.command == "t1":
            nodes = retrieval.assemble_t1(connection, scope=args.scope)
            if args.render:
                sys.stdout.write(retrieval.render_t1(nodes) + "\n")
            else:
                _emit(nodes)

        elif args.command == "stale":
            store.set_stale(connection, args.title)
            _emit({"stale": args.title})

        elif args.command == "rollback":
            _emit(store.rollback_run(connection, args.capture_run_id))

        elif args.command == "ingest":
            from . import ingest as ingest_module

            _emit(ingest_module.ingest_path(
                connection, args.path, scope=args.scope, dry_run=args.dry_run,
            ))

        elif args.command == "sql":
            connection.set_authorizer(_deny_writes)
            try:
                rows = connection.execute(args.query).fetchall()
            finally:
                connection.set_authorizer(None)
            _emit([dict(row) for row in rows])

        elif args.command == "snapshot":
            _emit({"snapshot": str(db.snapshot(connection, args.destination))})

    except InvariantError as error:
        _emit({"error": "invariant", "detail": str(error)})
        return 2
    except sqlite3.DatabaseError as error:
        _emit({"error": "database", "detail": str(error)})
        return 3
    finally:
        connection.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
