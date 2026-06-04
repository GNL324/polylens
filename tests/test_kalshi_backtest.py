import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.analysis.kalshi_backtest import export_kalshi_backtest, run_kalshi_backtest, summarize_kalshi_backtests
from src.adapters.kalshi import KalshiAuthConfig, KalshiAuthenticatedClient
from src.storage.kalshi_market_data import KalshiMarketDataStore


def _seed_db(path: Path):
    store = KalshiMarketDataStore(path)
    start = datetime(2026, 6, 4, tzinfo=timezone.utc)
    rows = [
        ("KXBTC", "BTC", 0.02, 0.08),
        ("KXBTC", "BTC", 0.04, 0.02),
        ("KXBTC", "BTC", 0.07, 0.01),
        ("KXETH", "ETH", 0.50, 0.12),
        ("KXETH", "ETH", 0.58, 0.04),
        ("KXETH", "ETH", 0.53, 0.02),
        ("KXSOL", "SOL", 0.96, 0.03),
        ("KXSOL", "SOL", 0.94, 0.02),
    ]
    for i, (ticker, asset, mid, spread) in enumerate(rows):
        ts = (start + timedelta(minutes=i)).isoformat()
        store.save_market_snapshot({"timestamp": ts, "ticker": ticker, "asset": asset, "market_type": "crypto", "title": ticker, "yes_bid": mid - spread/2, "yes_ask": mid + spread/2, "spread": spread, "liquidity": 10, "raw_json": "{}"})
        store.save_orderbook_snapshot({"timestamp": ts, "ticker": ticker, "yes_bid": mid - spread/2, "yes_ask": mid + spread/2, "spread": spread, "raw_json": "{}"})
        store.save_price_point({"timestamp": ts, "ticker": ticker, "asset": asset, "market_type": "crypto", "yes_bid": mid - spread/2, "yes_ask": mid + spread/2, "mid_price": mid, "spread": spread, "liquidity": 10})


def test_run_kalshi_backtest_all_strategies(tmp_path):
    db = tmp_path / "kalshi.db"
    _seed_db(db)
    report = run_kalshi_backtest(str(db), strategy="all", fee_assumption=0.001, spread_threshold=0.05, bankroll=1000)
    assert report["data"]["price_points"] == 8
    names = {row["strategy"] for row in report["strategies"]}
    assert names == {"spread-compression", "momentum", "mean-reversion", "probability-extremes"}
    assert report["summary"]["best_strategy"]
    assert report["summary"]["highest_spread_markets"]


def test_backtest_export(tmp_path):
    db = tmp_path / "kalshi.db"
    _seed_db(db)
    report = run_kalshi_backtest(str(db), strategy="all")
    files = export_kalshi_backtest(report, tmp_path)
    assert Path(files["json"]).exists()
    assert Path(files["csv"]).exists()
    assert "strategy" in Path(files["csv"]).read_text(encoding="utf-8")


def test_backtest_summary_ranks_assets(tmp_path):
    db = tmp_path / "kalshi.db"
    _seed_db(db)
    summary = summarize_kalshi_backtests(str(db))
    assert summary["best_strategy"]
    assert summary["best_asset"]
    assert summary["most_volatile_markets"]


def test_kalshi_backtest_cli_smoke(tmp_path, capsys):
    from src.cli import kalshi_backtest
    db = tmp_path / "kalshi.db"
    _seed_db(db)
    result = kalshi_backtest(db_path=str(db), strategy="all", export=True, as_json=False)
    assert result["strategies"]
    assert "Kalshi Backtest" in capsys.readouterr().out


def test_kalshi_backtest_summary_cli_smoke(tmp_path):
    from src.cli import kalshi_backtest_summary
    db = tmp_path / "kalshi.db"
    _seed_db(db)
    result = kalshi_backtest_summary(db_path=str(db), as_json=True)
    assert result["best_strategy"]


def test_write_endpoints_still_blocked(tmp_path):
    key_path = tmp_path / "kalshi.key"
    key_path.write_text("fake", encoding="utf-8")
    client = KalshiAuthenticatedClient(KalshiAuthConfig(api_key_id="id", private_key_path=key_path, env="production"), raw_dir=tmp_path)
    assert client.place_order()["mode"] == "write_blocked"
    assert client.cancel_order("order")["mode"] == "write_blocked"
