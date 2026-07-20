"""Database connection and schema management."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import sqlite_vec

SCHEMA_PATH = Path(__file__).with_name("schema.sql")

DEFAULT_DB_PATH = Path(
    os.environ.get("CLAUDE_MEMORY_DB", Path.home() / ".claude" / "memory" / "memory.db")
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


def init_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    connection.commit()


def snapshot(connection: sqlite3.Connection, destination: Path | str) -> Path:
    """Atomic, consistent backup. The store is precious; the indexes are not."""
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    connection.execute("VACUUM INTO ?", (str(destination),))
    return destination
