"""Agent-facing entry point. There is deliberately no user-facing CLI:
the user talks to the agent, and the agent calls this.

    python -m claude_memory search "michigan housing"
    python -m claude_memory dig "Ann Arbor apartment"
    python -m claude_memory context "Ann Arbor apartment"
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


def _use_utf8_stdout() -> None:
    """Force UTF-8 on the streams, whatever the console's codepage is.

    Windows defaults stdout to cp1252, which cannot encode the CJK and typographic
    characters that are all over Eric's wiki, so any search whose hits contain them
    died with UnicodeEncodeError before printing. The caller is a program reading
    a pipe, not a terminal rendering glyphs, so UTF-8 is always the right answer
    here; `errors="replace"` keeps a stray unencodable character from taking the
    whole command down.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")


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
    _use_utf8_stdout()
    parser = argparse.ArgumentParser(prog="claude_memory")
    parser.add_argument("--db", default=None, help="path to the SQLite store")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("init", help="create the store and apply the schema")

    remember_parser = subparsers.add_parser("remember", help="insert or supersede nodes")
    remember_parser.add_argument("--file", default=None, help="path to a JSON array")
    remember_parser.add_argument("--json", default=None, help="inline JSON array")
    remember_parser.add_argument("--run-id", default=None)

    search_parser = subparsers.add_parser("search", help="hybrid BM25 + vector retrieval")
    # Several queries in one call: a tool invocation is billed for its cached
    # prefix once, so N searches in one call cost a fraction of N calls. Finding
    # a node usually takes more than one angle, so this is the normal shape.
    search_parser.add_argument("query", nargs="+")
    search_parser.add_argument("--scope", default=None)
    search_parser.add_argument("--limit", type=int, default=10)
    search_parser.add_argument("--parent", default=None, help="dig down into an abstraction")
    search_parser.add_argument("--type", default=None, dest="node_type",
                               help="restrict to one node type, e.g. code-pref")
    search_parser.add_argument("--json", action="store_true",
                               help="raw rows with ranks, instead of the rendered block")
    search_parser.add_argument("--include-autoloaded", action="store_true",
                               help="do not filter out what T1 already loaded")

    dig_parser = subparsers.add_parser(
        "dig", help="expand nodes by id or claim; several handles in one call"
    )
    dig_parser.add_argument("handle", nargs="+")
    dig_parser.add_argument("--json", action="store_true")

    context_parser = subparsers.add_parser(
        "context", help="show the transcript around a session-sourced node's origin"
    )
    context_parser.add_argument("title")
    context_parser.add_argument("--window", type=int, default=3,
                                help="messages of context on each side of the hit")
    context_parser.add_argument("--json", action="store_true")

    t1_parser = subparsers.add_parser("t1", help="assemble the autoload set")
    t1_parser.add_argument("--scope", default=None)
    t1_parser.add_argument("--render", action="store_true", help="markdown, not JSON")

    stale_parser = subparsers.add_parser("stale", help="mark a node no longer true")
    stale_parser.add_argument("title", help="node title (an id also works)")

    rollback_parser = subparsers.add_parser("rollback", help="undo a capture run")
    rollback_parser.add_argument("capture_run_id")

    # `ingest` is gone for 0.1.0 along with the `raw` type it produced. The
    # module stays in the tree untouched: 0.2.0 revives it, and re-ingest is
    # lossless now that nothing refines a raw node into something worth keeping.

    migrate_parser = subparsers.add_parser(
        "migrate", help="upgrade an older store to this version"
    )
    migrate_parser.add_argument(
        "--next", type=int, default=0, metavar="N", dest="next_n",
        help="print up to N nodes still awaiting a claim",
    )
    migrate_parser.add_argument("--set", default=None, dest="set_file",
                                help="path to a JSON array of {id, claim, detail}")
    migrate_parser.add_argument("--status", action="store_true")
    migrate_parser.add_argument("--rollback", action="store_true")

    where_parser = subparsers.add_parser(
        "where", help="print resolved paths for the store and the settings files"
    )
    where_parser.add_argument("--code", action="store_true",
                              help="just the code conventions path")

    sql_parser = subparsers.add_parser("sql", help="read-only query escape hatch")
    sql_parser.add_argument("query")

    snapshot_parser = subparsers.add_parser("snapshot", help="consistent backup")
    snapshot_parser.add_argument("destination")

    daemon_parser = subparsers.add_parser(
        "daemon", help="the background process that keeps the embedding model warm"
    )
    daemon_parser.add_argument("action", choices=["start", "stop", "status"])

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
            excluded = (
                None if args.include_autoloaded
                else retrieval.t1_ids(connection, scope=args.scope)
            )
            results = [
                (query, retrieval.search(
                    connection, query, limit=args.limit,
                    scope=args.scope, parent=args.parent, node_type=args.node_type,
                    exclude_ids=excluded,
                ))
                for query in args.query
            ]
            if args.json:
                _emit(results if len(results) > 1 else results[0][1])
            else:
                # Labelled only when there is more than one, so the single-query
                # case stays exactly as terse as it was.
                blocks = [
                    retrieval.render_context(
                        hits,
                        heading=f"# memory that could be useful ({query}):"
                        if len(results) > 1 else "# memory that could be useful:",
                    )
                    for query, hits in results
                ]
                sys.stdout.write("\n\n".join(b for b in blocks if b) + "\n")

        elif args.command == "dig":
            nodes = [(handle, retrieval.dig(connection, handle))
                     for handle in args.handle]
            if args.json:
                _emit([node for _, node in nodes]
                      if len(nodes) > 1 else nodes[0][1])
            else:
                sys.stdout.write(retrieval.render_digs(nodes) + "\n")
            if any(node is None for _, node in nodes):
                return 4

        elif args.command == "context":
            excerpt = retrieval.dig_context(connection, args.title, window=args.window)
            if args.json:
                _emit(excerpt)
            else:
                sys.stdout.write(retrieval.render_transcript(excerpt, args.title))
            if excerpt is None:
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

        elif args.command == "migrate":
            from . import migrate as migrate_module

            if args.rollback:
                _emit(migrate_module.rollback(args.db))
            elif args.status:
                _emit(migrate_module.status(connection))
            elif args.set_file:
                _emit(migrate_module.set_claims(
                    connection, _read_specs(None, args.set_file)
                ))
            elif args.next_n:
                _emit(migrate_module.next_batch(connection, args.next_n))
            else:
                _emit(migrate_module.run(connection, args.db))

        elif args.command == "where":
            from . import settings as settings_module

            if args.code:
                sys.stdout.write(str(settings_module.path_for("code")) + "\n")
            else:
                _emit({
                    "db": str(Path(args.db) if args.db else db.DEFAULT_DB_PATH),
                    "settings": str(settings_module.settings_dir()),
                    **{name: str(settings_module.path_for(name))
                       for name in settings_module.FILES},
                })

        elif args.command == "sql":
            connection.set_authorizer(_deny_writes)
            try:
                rows = connection.execute(args.query).fetchall()
            finally:
                connection.set_authorizer(None)
            _emit([dict(row) for row in rows])

        elif args.command == "snapshot":
            _emit({"snapshot": str(db.snapshot(connection, args.destination))})

        elif args.command == "daemon":
            from . import daemon as daemon_module

            connection.close()  # this command never touches the store
            if args.action == "start":
                already = daemon_module.discover_running() is not None
                daemon_module.ensure_running()
                _emit({"daemon": "already running" if already else "starting"})
            elif args.action == "stop":
                pid = daemon_module.stop()
                _emit({"daemon": f"stopped pid {pid}" if pid else "not running"})
            else:
                port = daemon_module.discover_running()
                _emit({"daemon": f"running on port {port}" if port else "not running"})
            return 0

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
