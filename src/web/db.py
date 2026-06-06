from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

POLYLENS_DB_PATH = Path("data/polylens.db")
OPPORTUNITIES_DB_PATH = Path("data/opportunities.db")


def connect(db_path: str | Path) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)).fetchone()
    return row is not None


def rows_or_empty(db_path: str | Path, table: str, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    with connect(db_path) as conn:
        if not table_exists(conn, table):
            return []
        return [dict(row) for row in conn.execute(sql, params)]


def load_json(value: Any, default: Any = None) -> Any:
    if value in (None, ""):
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return default


def ensure_trades_table(db_path: str | Path = POLYLENS_DB_PATH) -> None:
    with connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY,
                mode TEXT,
                opened_at TEXT,
                closed_at TEXT,
                platform TEXT,
                market_id TEXT,
                market_title TEXT,
                strategy TEXT,
                market_type TEXT,
                side TEXT,
                entry_price REAL,
                exit_price REAL,
                stake REAL,
                quantity REAL,
                gross_pnl REAL,
                fees REAL,
                net_pnl REAL,
                roi REAL,
                status TEXT,
                notes TEXT,
                raw_json TEXT
            )
            """
        )

