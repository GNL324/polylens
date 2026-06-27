from __future__ import annotations

import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import pytest
import sqlite3

from src.testing.runtime_db_redirect import (
    PRODUCTION_RUNTIME_DBS,
    assert_runtime_db_redirect_applied,
    build_isolated_runtime_db_redirect,
    install_runtime_db_redirect,
)


def isolated_signal_db(tmp_path: Path) -> Path:
    """Return a per-test trader signal DB path under pytest's tmp_path."""
    return tmp_path / "signals.db"


def _runtime_isolated_paths(tmp_path: Path) -> dict[Path, Path]:
    return {
        production_path: tmp_path / f"isolated_{name}.db"
        for name, production_path in PRODUCTION_RUNTIME_DBS.items()
    }


def _patch_closing_connection_imports(monkeypatch, isolated_closing_connection, original_closing_connection) -> None:
    """Patch stale ``from src.sqlite_utils import closing_connection`` bindings."""
    for module in list(sys.modules.values()):
        if module is None:
            continue
        bound = getattr(module, "closing_connection", None)
        if bound is None:
            continue
        if bound is original_closing_connection or getattr(bound, "__module__", None) == "src.sqlite_utils":
            monkeypatch.setattr(module, "closing_connection", isolated_closing_connection, raising=False)


@pytest.fixture(scope="session", autouse=True)
def _session_runtime_db_redirect(tmp_path_factory):
    """Keep redirect resolver active for the entire pytest session."""
    session_tmp = tmp_path_factory.mktemp("runtime_db_isolation")
    isolated_paths = _runtime_isolated_paths(session_tmp)
    install_runtime_db_redirect(build_isolated_runtime_db_redirect(isolated_paths))
    yield isolated_paths


@pytest.fixture
def signal_db_path(tmp_path) -> Path:
    return isolated_signal_db(tmp_path)


@pytest.fixture(autouse=True)
def _isolate_runtime_sqlite_dbs(tmp_path, monkeypatch, _session_runtime_db_redirect):
    """Redirect accidental opens of runtime data/*.db files to per-test temp files."""
    from src import sqlite_utils

    isolated_paths = _runtime_isolated_paths(tmp_path)
    redirect = build_isolated_runtime_db_redirect(isolated_paths)
    original_closing_connection = sqlite_utils.closing_connection
    original_sqlite3_connect = sqlite3.connect

    install_runtime_db_redirect(redirect)

    @contextmanager
    def isolated_closing_connection(
        db_path: str | Path,
        *,
        row_factory: type | None = sqlite3.Row,
    ) -> Iterator[sqlite3.Connection]:
        with original_closing_connection(db_path, row_factory=row_factory) as conn:
            yield conn

    def isolated_sqlite3_connect(database, *args, **kwargs):
        target = redirect(database)
        assert_runtime_db_redirect_applied(database)
        return original_sqlite3_connect(target, *args, **kwargs)

    monkeypatch.setattr(sqlite_utils, "closing_connection", isolated_closing_connection)
    monkeypatch.setattr(sqlite3, "connect", isolated_sqlite3_connect)
    _patch_closing_connection_imports(monkeypatch, isolated_closing_connection, original_closing_connection)

    yield isolated_paths
    install_runtime_db_redirect(build_isolated_runtime_db_redirect(_session_runtime_db_redirect))


@pytest.fixture
def isolated_runtime_db_paths(_isolate_runtime_sqlite_dbs) -> dict[str, Path]:
    return {
        name: _isolate_runtime_sqlite_dbs[path]
        for name, path in PRODUCTION_RUNTIME_DBS.items()
    }
