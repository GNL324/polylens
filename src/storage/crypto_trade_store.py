from __future__ import annotations
import json, sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator
from src.sqlite_utils import closing_connection
DEFAULT_DB_PATH = "data/polylens.db"
class CryptoTradeStore:
    def __init__(self, db_path=DEFAULT_DB_PATH):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
    def initialize(self):
        with self._connect() as conn:
            conn.executescript("""
            CREATE TABLE IF NOT EXISTS crypto_price_ticks (id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT NOT NULL, symbol TEXT, source TEXT, bid REAL, ask REAL, mid REAL, last REAL, latency_ms REAL, raw_json TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS crypto_markets (id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT NOT NULL, asset TEXT, venue TEXT, ticker TEXT, start_ts REAL, end_ts REAL, direction TEXT, yes_bid REAL, yes_ask REAL, no_bid REAL, no_ask REAL, liquidity REAL, strike_price REAL, reference_price REAL, window_minutes INTEGER, raw_json TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS crypto_signals (id INTEGER PRIMARY KEY AUTOINCREMENT, scan_run_id INTEGER, timestamp TEXT NOT NULL, asset TEXT, window_minutes INTEGER, direction TEXT, venue TEXT, ticker TEXT, spot_price REAL, implied_prob REAL, model_prob REAL, edge REAL, roi REAL, meta_json TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS crypto_trades (id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT NOT NULL, asset TEXT, window_minutes INTEGER, direction TEXT, venue TEXT, ticker TEXT, price REAL, count INTEGER, stake REAL, mode TEXT, status TEXT, order_json TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS crypto_decisions (id INTEGER PRIMARY KEY AUTOINCREMENT, scan_run_id INTEGER, timestamp TEXT NOT NULL, asset TEXT, window_minutes INTEGER, direction TEXT, venue TEXT, ticker TEXT, accepted INTEGER, reason TEXT, meta_json TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS crypto_errors (id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT NOT NULL, context_json TEXT NOT NULL, error TEXT NOT NULL);
            """)
    def save_price_tick(self, symbol, source, tick, raw):
        with self._connect() as conn:
            conn.execute("INSERT INTO crypto_price_ticks (timestamp, symbol, source, bid, ask, mid, last, latency_ms, raw_json) VALUES (?,?,?,?,?,?,?,?,?)", (_now(), symbol, source, _n(tick.get("bid")), _n(tick.get("ask")), _n(tick.get("mid")), _n(tick.get("last")), _n(tick.get("latency_ms")), _j(raw)))
    def save_market(self, market, window_minutes=None):
        with self._connect() as conn:
            conn.execute("INSERT INTO crypto_markets (timestamp, asset, venue, ticker, start_ts, end_ts, direction, yes_bid, yes_ask, no_bid, no_ask, liquidity, strike_price, reference_price, window_minutes, raw_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (_now(), market.asset, market.venue, market.ticker, market.start_ts, market.end_ts, market.direction, _n(market.yes_bid), _n(market.yes_ask), _n(market.no_bid), _n(market.no_ask), _n(market.liquidity), _n(getattr(market, "strike_price", None)), _n(getattr(market, "reference_price", None)), int(window_minutes) if window_minutes is not None else None, _j(getattr(market, "raw", None) or {})))
    def save_signal(self, signal, scan_run_id=None):
        with self._connect() as conn:
            conn.execute("INSERT INTO crypto_signals (scan_run_id, timestamp, asset, window_minutes, direction, venue, ticker, spot_price, implied_prob, model_prob, edge, roi, meta_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", (scan_run_id, _now(), signal.asset, int(signal.window_minutes), signal.direction, signal.venue, signal.ticker, float(signal.spot_price), float(signal.implied_prob), float(signal.model_prob), float(signal.edge), float(signal.roi), _j(signal.meta or {})))
    def save_trade(self, trade):
        with self._connect() as conn:
            conn.execute("INSERT INTO crypto_trades (timestamp, asset, window_minutes, direction, venue, ticker, price, count, stake, mode, status, order_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (_now(), trade["asset"], int(trade["window_minutes"]), trade["direction"], trade["venue"], trade["ticker"], float(trade["price"]), int(trade["count"]), float(trade["stake"]), str(trade["mode"]), str(trade.get("status","submitted")), _j(trade.get("order") or {})))
    def save_decision(self, decision, scan_run_id=None):
        with self._connect() as conn:
            conn.execute("INSERT INTO crypto_decisions (scan_run_id, timestamp, asset, window_minutes, direction, venue, ticker, accepted, reason, meta_json) VALUES (?,?,?,?,?,?,?,?,?,?)", (scan_run_id, _now(), decision["asset"], int(decision["window_minutes"]), decision["direction"], decision["venue"], decision["ticker"], 1 if decision.get("accepted") else 0, decision.get("reason"), _j(decision.get("meta") or {})))
    def save_error(self, context, error):
        with self._connect() as conn:
            conn.execute("INSERT INTO crypto_errors (timestamp, context_json, error) VALUES (?,?,?)", (_now(), _j(context), error))
    def replay_ticks(self, limit=500):
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM crypto_price_ticks ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]
    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        with closing_connection(self.db_path, row_factory=None) as conn:
            yield conn
def _n(v):
    if v is None or v == "":
        return None
    try:
        return float(v)
    except Exception:
        return None
def _j(v):
    return json.dumps(v, sort_keys=True, default=str)
def _now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00","Z")
