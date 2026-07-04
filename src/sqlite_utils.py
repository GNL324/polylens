from __future__ import annotations

import sqlite3 as _sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from src.testing.runtime_db_redirect import assert_runtime_db_redirect_applied, resolve_runtime_db_target


@contextmanager
def closing_connection(
    db_path: str | Path,
    *,
    row_factory: type | None = _sqlite3.Row,
) -> Iterator[_sqlite3.Connection]:
    """Open sqlite3 and always close on exit.

    Unlike ``with sqlite3.connect(...) as conn``, this closes the connection
    instead of only committing/rolling back the implicit transaction.
    """
    path = Path(resolve_runtime_db_target(db_path))
    assert_runtime_db_redirect_applied(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = _sqlite3.connect(path, timeout=30.0)
    conn.execute("PRAGMA journal_mode=WAL")
    if row_factory is not None:
        conn.row_factory = row_factory
    try:
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()
