"""Database connection and schema management."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import sqlite_vec

from . import settings

SCHEMA_PATH = Path(__file__).with_name("schema.sql")

# The store lives beside the pref files, not under ~/.claude/. That directory
# belongs to the Claude Code harness; everything this project owns -- rules and
# nodes alike -- is project-managed and moves together. v0 split them, so a
# re-clone kept the database and lost the prefs.
LEGACY_DB_PATH = Path.home() / ".claude" / "memory" / "memory.db"

DEFAULT_DB_PATH = Path(
    os.environ.get("CLAUDE_MEMORY_DB") or settings.settings_dir() / "memory.db"
)


def connect(db_path: Path | str | None = None) -> sqlite3.Connection:
    """Open a connection with sqlite-vec loaded and the schema applied."""
    path = Path(db_path) if db_path is not None else DEFAULT_DB_PATH
    if str(path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)

    connection = sqlite3.connect(str(path))
    connection.row_factory = sqlite3.Row

    try:
        connection.enable_load_extension(True)
        sqlite_vec.load(connection)
    except AttributeError as error:  # pragma: no cover - platform dependent
        raise RuntimeError(
            "This Python build cannot load SQLite extensions, which sqlite-vec "
            "requires. Install pysqlite3-binary or use a Python built with "
            "--enable-loadable-sqlite-extensions."
        ) from error
    finally:
        try:
            connection.enable_load_extension(False)
        except AttributeError:
            pass

    init_schema(connection)
    return connection


SCHEMA_VERSION = 1


def user_version(connection: sqlite3.Connection) -> int:
    return connection.execute("PRAGMA user_version").fetchone()[0]


def needs_migration(connection: sqlite3.Connection) -> bool:
    return user_version(connection) < SCHEMA_VERSION


def init_schema(connection: sqlite3.Connection) -> None:
    """Apply the schema, and stamp the version only on a store that is new.

    Every statement in schema.sql is IF NOT EXISTS, so applying it to a v0 store
    silently succeeds while changing nothing -- the old (title, summary) columns
    survive untouched. That makes the version stamp the only reliable signal
    that a store has actually been converted, so it must never be written as a
    side effect of connecting. A store with no `nodes` table is genuinely new
    and gets stamped; anything else has to earn it through migrate.
    """
    fresh = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'nodes'"
    ).fetchone() is None

    if not fresh and needs_migration(connection):
        # A v0 store cannot take this file at all -- `CREATE INDEX ... ON
        # nodes(claim)` fails outright on a table that still has (title,
        # summary). Applying nothing is the correct answer: migrate owns the
        # conversion, and every other command already refuses to run against an
        # un-migrated store rather than half-working.
        return

    connection.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    if fresh:
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    connection.commit()


def snapshot(connection: sqlite3.Connection, destination: Path | str) -> Path:
    """Atomic, consistent backup. The store is precious; the indexes are not."""
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    connection.execute("VACUUM INTO ?", (str(destination),))
    return destination
