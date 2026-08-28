"""The one way to open dispatch.db, and it is read-only.

Owned by: the db layer. Called by `schema.py`, `executor.py` and tests. Calls:
`config` only.

Three independent things make this connection unable to write, because CLAUDE.md
section 3.3 asks that even a hallucinated DELETE be stopped by SQLite itself
rather than by our own parsing:

  1. the URI flag `mode=ro`   -- the file is opened read-only by the OS layer
  2. `PRAGMA query_only=ON`   -- the connection refuses write statements
  3. the guard's AST check    -- a non-SELECT never reaches here (src/db/guard.py)

Layers 1 and 2 hold even if layer 3 is bypassed entirely, which is the property
worth having: the parser is the part most likely to have a hole in it.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from src import config


class DatabaseUnavailableError(RuntimeError):
    """dispatch.db is missing or cannot be opened read-only."""


def _connect(db_path: Path) -> sqlite3.Connection:
    if not db_path.exists():
        raise DatabaseUnavailableError(f"Dispatch database not found at {db_path}")

    # mode=ro makes SQLite itself refuse writes. A plain path would create an empty
    # database if the file were missing, which is why the existence check above
    # runs first -- silently querying an empty db is the worst failure mode here.
    connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON;")
    return connection


@contextmanager
def read_only_connection(db_path: Path | None = None) -> Iterator[sqlite3.Connection]:
    """Yield a read-only connection and always close it."""
    connection = _connect(db_path or config.DISPATCH_DB_PATH)
    try:
        yield connection
    finally:
        connection.close()
