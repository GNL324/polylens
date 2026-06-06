from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

from src.analysis.market_regime_analysis import classify_market_regimes, run_market_regime_analysis


def _write_series(db_path, ticker: str, prices: list[float]) -> None:
    with sqlite3.connect(db_path) as conn:
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
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        for index, price in enumerate(prices):
            conn.execute(
                """
                INSERT INTO kalshi_price_series
                (timestamp, ticker, asset, market_type, yes_bid, yes_ask, mid_price, spread, liquidity)
                VALUES (?, ?, 'BTC', 'crypto', ?, ?, ?, 0.02, 100)
                """,
                ((start + timedelta(minutes=index)).isoformat(), ticker, price - 0.01, price + 0.01, price),
            )


def test_classification_detects_trending_ranging_and_volatile_regimes():
    trending = [{"mid_price": price} for price in [0.2, 0.23, 0.27, 0.31, 0.36, 0.42]]
    ranging = [{"mid_price": price} for price in [0.48, 0.5, 0.49, 0.51, 0.5, 0.49]]
    volatile = [{"mid_price": price} for price in [0.2, 0.8, 0.25, 0.75, 0.3, 0.7]]

    assert "trending_up" in classify_market_regimes(trending)
    assert "ranging" in classify_market_regimes(ranging)
    assert "chop/noise" in classify_market_regimes(ranging)
    assert "high_volatility" in classify_market_regimes(volatile)


def test_low_sample_size_returns_insufficient_data(tmp_path):
    db_path = tmp_path / "kalshi_market_data.db"
    _write_series(db_path, "KXSMALL", [0.4, 0.41, 0.42])

    result = run_market_regime_analysis(str(db_path), export=False)
    trending = next(row for row in result["regimes"] if row["regime"] == "trending_up")
    momentum = next(row for row in trending["strategies"] if row["strategy"] == "momentum")

    assert momentum["trade_count"] < 5
    assert momentum["confidence_level"] == "insufficient_data"


def test_strategies_ranked_correctly_for_trending_and_ranging(tmp_path):
    db_path = tmp_path / "kalshi_market_data.db"
    _write_series(db_path, "KXTREND", [0.2, 0.23, 0.26, 0.3, 0.35, 0.41, 0.48, 0.56])
    _write_series(db_path, "KXRANGE", [0.45, 0.5, 0.45, 0.5, 0.45, 0.5, 0.45])
    _write_series(db_path, "KXVOL", [0.2, 0.8, 0.25, 0.75, 0.3, 0.7, 0.35, 0.65])

    result = run_market_regime_analysis(str(db_path), export=False)

    trending = next(row for row in result["regimes"] if row["regime"] == "trending_up")
    ranging = next(row for row in result["regimes"] if row["regime"] == "ranging")
    high_vol = next(row for row in result["regimes"] if row["regime"] == "high_volatility")

    assert trending["strategies"][0]["strategy"] == "momentum"
    assert ranging["strategies"][0]["strategy"] == "mean-reversion"
    assert high_vol["strategies"][0]["strategy"] in {"mean-reversion", "opposite-side"}
    assert result["summary"]["conditions_that_would_have_worked"]


def test_export_writes_market_regime_json_and_csv(tmp_path, monkeypatch):
    db_path = tmp_path / "kalshi_market_data.db"
    report_dir = tmp_path / "data" / "reports"
    _write_series(db_path, "KXTREND", [0.2, 0.23, 0.26, 0.3, 0.35, 0.41, 0.48, 0.56])
    monkeypatch.chdir(tmp_path)

    result = run_market_regime_analysis(str(db_path), export=True)

    assert result["files"]["json"] == "data/reports/market_regime_analysis.json"
    assert result["files"]["csv"] == "data/reports/market_regime_analysis.csv"
    assert (report_dir / "market_regime_analysis.json").exists()
    assert (report_dir / "market_regime_analysis.csv").read_text(encoding="utf-8").startswith("regime,rank,strategy")
