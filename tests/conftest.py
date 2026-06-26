from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import pytest
import sqlite3

REPO_ROOT = Path(__file__).resolve().parents[1]
PRODUCTION_TRADER_SIGNALS_DB = (REPO_ROOT / "data" / "trader_signals.db").resolve()


def isolated_signal_db(tmp_path: Path) -> Path:
    """Return a per-test trader signal DB path under pytest's tmp_path."""
    return tmp_path / "signals.db"


def _resolve_trader_signal_db_path(db_path: str | Path, isolated_path: Path) -> Path:
    candidate = Path(db_path)
    try:
        resolved = candidate.resolve()
    except OSError:
        resolved = candidate
    if resolved == PRODUCTION_TRADER_SIGNALS_DB:
        return isolated_path
    return candidate


@pytest.fixture
def signal_db_path(tmp_path) -> Path:
    return isolated_signal_db(tmp_path)


@pytest.fixture(autouse=True)
def _isolate_production_trader_signals_db(tmp_path, monkeypatch):
    """Redirect accidental opens of data/trader_signals.db to a per-test temp file."""
    from src import sqlite_utils

    isolated_path = tmp_path / "isolated_trader_signals.db"
    original_closing_connection = sqlite_utils.closing_connection
    original_sqlite3_connect = sqlite3.connect

    @contextmanager
    def isolated_closing_connection(
        db_path: str | Path,
        *,
        row_factory: type | None = sqlite3.Row,
    ) -> Iterator[sqlite3.Connection]:
        resolved_path = _resolve_trader_signal_db_path(db_path, isolated_path)
        with original_closing_connection(resolved_path, row_factory=row_factory) as conn:
            yield conn

    def isolated_sqlite3_connect(database, *args, **kwargs):
        resolved_path = _resolve_trader_signal_db_path(database, isolated_path)
        return original_sqlite3_connect(resolved_path, *args, **kwargs)

    monkeypatch.setattr(sqlite_utils, "closing_connection", isolated_closing_connection)
    monkeypatch.setattr(sqlite3, "connect", isolated_sqlite3_connect)

    yield isolated_path
