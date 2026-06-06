from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

import src.cli as cli
from src.analysis.kalshi_account_history_export import export_kalshi_account_history
from src.analysis.kalshi_edge_analysis import run_kalshi_edge_analysis
from src.analysis.market_regime_analysis import run_market_regime_analysis


class FakeKalshiClient:
    def get_balance(self):
        return {"balance": 10000}

    def get_positions(self):
        return {"positions": [{"ticker": "KXTEST", "position": 1}]}

    def get_orders(self):
        return {"orders": [{"ticker": "KXTEST", "status": "filled", "side": "yes", "price": 40, "count": 10}]}

    def get_fills(self):
        return {"fills": _fills()}


def _fills():
    return [
        {"ticker": "KXTEST-1", "side": "yes", "price": 40, "count": 10, "created_time": "2026-01-01T00:00:00Z", "outcome": "yes"},
        {"ticker": "KXTEST-2", "side": "yes", "price": 55, "count": 10, "created_time": "2026-01-01T00:10:00Z", "outcome": "no"},
        {"ticker": "KXTEST-3", "side": "no", "price": 30, "count": 10, "created_time": "2026-01-01T00:20:00Z", "outcome": "yes"},
        {"ticker": "KXTEST-4", "side": "yes", "price": 35, "count": 10, "created_time": "2026-01-01T00:30:00Z", "outcome": "yes"},
        {"ticker": "KXTEST-5", "side": "no", "price": 45, "count": 10, "created_time": "2026-01-01T00:40:00Z", "outcome": "no"},
    ]


def _write_account_history(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"balance": {}, "positions": {"positions": []}, "orders": {"orders": []}, "fills": {"fills": _fills()}}),
        encoding="utf-8",
    )


def _write_price_db(path):
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            CREATE TABLE kalshi_price_series (
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
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        for ticker in [f"KXTEST-{index}" for index in range(1, 6)]:
            for offset, price in enumerate([0.35, 0.4, 0.45, 0.5, 0.55, 0.6]):
                conn.execute(
                    """
                    INSERT INTO kalshi_price_series
                    (timestamp, ticker, asset, market_type, yes_bid, yes_ask, no_bid, no_ask, mid_price, spread, liquidity)
                    VALUES (?, ?, 'BTC', 'crypto', ?, ?, ?, ?, ?, 0.02, 100)
                    """,
                    (
                        (start + timedelta(minutes=offset)).isoformat(),
                        ticker,
                        price - 0.01,
                        price + 0.01,
                        1 - price - 0.01,
                        1 - price + 0.01,
                        price,
                    ),
                )


def test_mocked_account_history_export_writes_local_file(tmp_path):
    output = tmp_path / "kalshi_account_history.json"

    result = export_kalshi_account_history(FakeKalshiClient(), output)

    assert result["accepted"] is True
    assert result["counts"]["fills"] == 5
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["fills"]["fills"][0]["ticker"] == "KXTEST-1"


def test_kalshi_export_account_history_auth_error_path(monkeypatch):
    class BrokenClient:
        def __init__(self, *args, **kwargs):
            raise cli.KalshiAuthConfigError("missing credentials")

    monkeypatch.setattr(cli, "KalshiAuthenticatedClient", BrokenClient)

    result = cli.kalshi_export_account_history(as_json=True)

    assert result == {"accepted": False, "mode": "auth_error", "reason": "missing credentials"}


def test_edge_analysis_writes_json_and_csv(tmp_path):
    account_path = tmp_path / "raw" / "kalshi_account_history.json"
    db_path = tmp_path / "kalshi_market_data.db"
    _write_account_history(account_path)
    _write_price_db(db_path)
    import os

    old_cwd = os.getcwd()
    try:
        os.chdir(tmp_path)
        result = run_kalshi_edge_analysis(account_history_path=account_path, snapshot_path=db_path, export=True)
    finally:
        os.chdir(old_cwd)

    assert result["summary"]["sample_size"] == 5
    assert result["files"]["json"] == "data/reports/kalshi_edge_analysis.json"
    assert result["files"]["csv"] == "data/reports/kalshi_edge_analysis.csv"
    assert (tmp_path / "data" / "reports" / "kalshi_edge_analysis.json").exists()
    assert (tmp_path / "data" / "reports" / "kalshi_edge_analysis.csv").read_text(encoding="utf-8").startswith("rank,variant")


def test_regime_analysis_consumes_edge_export(tmp_path, monkeypatch):
    account_path = tmp_path / "data" / "raw" / "kalshi_account_history.json"
    db_path = tmp_path / "data" / "kalshi_market_data.db"
    edge_path = tmp_path / "data" / "reports" / "kalshi_edge_analysis.json"
    _write_account_history(account_path)
    _write_price_db(db_path)
    monkeypatch.chdir(tmp_path)
    edge = run_kalshi_edge_analysis(account_history_path=account_path, snapshot_path=db_path, export=True)

    result = run_market_regime_analysis(str(db_path), account_history_path=account_path, edge_analysis_path=edge_path, export=False)

    assert edge["summary"]["classification"] == result["summary"]["old_trader_edge_classification"]
    assert result["edge_analysis_context"]["available"] is True
    assert result["account_context"]["available"] is True


def test_missing_history_produces_clear_insufficient_data_warning(tmp_path):
    db_path = tmp_path / "missing.db"

    result = run_kalshi_edge_analysis(account_history_path=tmp_path / "missing_history.json", snapshot_path=db_path, export=False)

    assert result["summary"]["classification"] == "insufficient_data"
    warnings = result["summary"]["data_quality_warnings"]
    assert any("account_history_missing" in warning for warning in warnings)
    assert any("insufficient_sample_size" in warning for warning in warnings)
