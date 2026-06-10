from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


@contextmanager
def closing_connection(
    db_path: str | Path,
    *,
    row_factory: type | None = sqlite3.Row,
) -> Iterator[sqlite3.Connection]:
    """Open sqlite3 and always close on exit.

    Unlike ``with sqlite3.connect(...) as conn``, this closes the connection
    instead of only committing/rolling back the implicit transaction.
    """
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
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
