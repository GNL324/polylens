from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

DEFAULT_KALSHI_DATA_DB = "data/kalshi_market_data.db"


class KalshiMarketDataStore:
    def __init__(self, db_path: str | Path = DEFAULT_KALSHI_DATA_DB) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.init_db()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def init_db(self) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS kalshi_market_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    ticker TEXT NOT NULL,
                    asset TEXT,
                    market_type TEXT,
                    title TEXT,
                    status TEXT,
                    yes_bid REAL,
                    yes_ask REAL,
                    no_bid REAL,
                    no_ask REAL,
                    spread REAL,
                    volume REAL,
                    liquidity REAL,
                    close_time TEXT,
                    expiration_time TEXT,
                    raw_json TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS kalshi_orderbook_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    ticker TEXT NOT NULL,
                    yes_bid REAL,
                    yes_bid_size REAL,
                    yes_ask REAL,
                    yes_ask_size REAL,
                    no_bid REAL,
                    no_bid_size REAL,
                    no_ask REAL,
                    no_ask_size REAL,
                    spread REAL,
                    raw_json TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS kalshi_price_series (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    ticker TEXT NOT NULL,
                    asset TEXT,
                    market_type TEXT,
                    yes_bid REAL,
                    yes_ask REAL,
                    no_bid REAL,
                    no_ask REAL,
                    mid_price REAL,
                    spread REAL,
                    liquidity REAL
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_kalshi_market_snapshots_ticker_time ON kalshi_market_snapshots(ticker, timestamp)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_kalshi_orderbook_snapshots_ticker_time ON kalshi_orderbook_snapshots(ticker, timestamp)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_kalshi_price_series_ticker_time ON kalshi_price_series(ticker, timestamp)")

    def save_market_snapshot(self, row: dict[str, Any]) -> None:
        keys = ["timestamp", "ticker", "asset", "market_type", "title", "status", "yes_bid", "yes_ask", "no_bid", "no_ask", "spread", "volume", "liquidity", "close_time", "expiration_time", "raw_json"]
        with self.connect() as conn:
            conn.execute(
                f"INSERT INTO kalshi_market_snapshots ({','.join(keys)}) VALUES ({','.join(['?'] * len(keys))})",
                [row.get(key) for key in keys],
            )

    def save_orderbook_snapshot(self, row: dict[str, Any]) -> None:
        keys = ["timestamp", "ticker", "yes_bid", "yes_bid_size", "yes_ask", "yes_ask_size", "no_bid", "no_bid_size", "no_ask", "no_ask_size", "spread", "raw_json"]
        with self.connect() as conn:
            conn.execute(
                f"INSERT INTO kalshi_orderbook_snapshots ({','.join(keys)}) VALUES ({','.join(['?'] * len(keys))})",
                [row.get(key) for key in keys],
            )

    def save_price_point(self, row: dict[str, Any]) -> None:
        keys = ["timestamp", "ticker", "asset", "market_type", "yes_bid", "yes_ask", "no_bid", "no_ask", "mid_price", "spread", "liquidity"]
        with self.connect() as conn:
            conn.execute(
                f"INSERT INTO kalshi_price_series ({','.join(keys)}) VALUES ({','.join(['?'] * len(keys))})",
                [row.get(key) for key in keys],
            )

    def summary(self) -> dict[str, Any]:
        with self.connect() as conn:
            market_count = conn.execute("SELECT COUNT(*) FROM kalshi_market_snapshots").fetchone()[0]
            orderbook_count = conn.execute("SELECT COUNT(*) FROM kalshi_orderbook_snapshots").fetchone()[0]
            price_count = conn.execute("SELECT COUNT(*) FROM kalshi_price_series").fetchone()[0]
            assets = [row[0] for row in conn.execute("SELECT DISTINCT asset FROM kalshi_market_snapshots WHERE asset IS NOT NULL AND asset != 'unknown' ORDER BY asset").fetchall()]
            liquid = [dict(row) for row in conn.execute("SELECT ticker, asset, market_type, MAX(COALESCE(liquidity, 0)) AS liquidity FROM kalshi_market_snapshots GROUP BY ticker, asset, market_type ORDER BY liquidity DESC LIMIT 10").fetchall()]
            avg_spread = conn.execute("SELECT AVG(spread) FROM kalshi_price_series WHERE spread IS NOT NULL").fetchone()[0]
            movement = [dict(row) for row in conn.execute(
                """
                SELECT ticker, MIN(mid_price) AS min_mid_price, MAX(mid_price) AS max_mid_price,
                       MAX(mid_price) - MIN(mid_price) AS movement, COUNT(*) AS points
                FROM kalshi_price_series
                WHERE mid_price IS NOT NULL
                GROUP BY ticker
                ORDER BY ABS(MAX(mid_price) - MIN(mid_price)) DESC
                LIMIT 10
                """
            ).fetchall()]
            missing = {
                "missing_yes_bid": conn.execute("SELECT COUNT(*) FROM kalshi_market_snapshots WHERE yes_bid IS NULL").fetchone()[0],
                "missing_yes_ask": conn.execute("SELECT COUNT(*) FROM kalshi_market_snapshots WHERE yes_ask IS NULL").fetchone()[0],
                "missing_liquidity": conn.execute("SELECT COUNT(*) FROM kalshi_market_snapshots WHERE liquidity IS NULL").fetchone()[0],
                "missing_close_time": conn.execute("SELECT COUNT(*) FROM kalshi_market_snapshots WHERE close_time IS NULL OR close_time = ''").fetchone()[0],
            }
        return {
            "db_path": str(self.db_path),
            "snapshots_collected": market_count,
            "orderbook_snapshots_collected": orderbook_count,
            "price_points_collected": price_count,
            "assets_tracked": assets,
            "most_liquid_markets": liquid,
            "average_spread": round(float(avg_spread), 4) if avg_spread is not None else None,
            "price_movement_by_ticker": movement,
            "missing_fields": missing,
        }
