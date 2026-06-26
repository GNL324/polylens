from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Iterator
from urllib.parse import unquote, urlsplit, urlunsplit

import pytest
import sqlite3

REPO_ROOT = Path(__file__).resolve().parents[1]
PRODUCTION_TRADER_SIGNALS_DB = (REPO_ROOT / "data" / "trader_signals.db").resolve()
PRODUCTION_RUNTIME_DBS = {
    "paper_trading": (REPO_ROOT / "data" / "paper_trading.db").resolve(),
    "trader_signals": PRODUCTION_TRADER_SIGNALS_DB,
    "traders": (REPO_ROOT / "data" / "traders.db").resolve(),
    "wallet_activity": (REPO_ROOT / "data" / "wallet_activity.db").resolve(),
}


def isolated_signal_db(tmp_path: Path) -> Path:
    """Return a per-test trader signal DB path under pytest's tmp_path."""
    return tmp_path / "signals.db"


def _runtime_isolated_paths(tmp_path: Path) -> dict[Path, Path]:
    return {
        production_path: tmp_path / f"isolated_{name}.db"
        for name, production_path in PRODUCTION_RUNTIME_DBS.items()
    }


def _resolve_runtime_db_path(db_path: str | Path, isolated_paths: dict[Path, Path]) -> str | Path:
    if isinstance(db_path, str) and db_path.startswith("file:"):
        return _resolve_runtime_db_uri(db_path, isolated_paths)
    candidate = Path(db_path)
    try:
        resolved = candidate.resolve()
    except OSError:
        resolved = candidate
    isolated = isolated_paths.get(resolved)
    if isolated is not None:
        return isolated
    return candidate


def _resolve_runtime_db_uri(uri: str, isolated_paths: dict[Path, Path]) -> str:
    parsed = urlsplit(uri)
    candidate = Path(unquote(parsed.path))
    try:
        resolved = candidate.resolve()
    except OSError:
        resolved = candidate
    isolated = isolated_paths.get(resolved)
    if isolated is None:
        return uri
    return urlunsplit((parsed.scheme, parsed.netloc, str(isolated), parsed.query, parsed.fragment))


@pytest.fixture
def signal_db_path(tmp_path) -> Path:
    return isolated_signal_db(tmp_path)


@pytest.fixture(autouse=True)
def _isolate_runtime_sqlite_dbs(tmp_path, monkeypatch):
    """Redirect accidental opens of runtime data/*.db files to per-test temp files."""
    from src import sqlite_utils

    isolated_paths = _runtime_isolated_paths(tmp_path)
    original_closing_connection = sqlite_utils.closing_connection
    original_sqlite3_connect = sqlite3.connect

    @contextmanager
    def isolated_closing_connection(
        db_path: str | Path,
        *,
        row_factory: type | None = sqlite3.Row,
    ) -> Iterator[sqlite3.Connection]:
        resolved_path = _resolve_runtime_db_path(db_path, isolated_paths)
        with original_closing_connection(resolved_path, row_factory=row_factory) as conn:
            yield conn

    def isolated_sqlite3_connect(database, *args, **kwargs):
        resolved_path = _resolve_runtime_db_path(database, isolated_paths)
        return original_sqlite3_connect(resolved_path, *args, **kwargs)

    monkeypatch.setattr(sqlite_utils, "closing_connection", isolated_closing_connection)
    monkeypatch.setattr(sqlite3, "connect", isolated_sqlite3_connect)

    yield isolated_paths


@pytest.fixture
def isolated_runtime_db_paths(_isolate_runtime_sqlite_dbs) -> dict[str, Path]:
    return {
        name: _isolate_runtime_sqlite_dbs[path]
        for name, path in PRODUCTION_RUNTIME_DBS.items()
    }
