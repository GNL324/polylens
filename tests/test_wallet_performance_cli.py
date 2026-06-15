from __future__ import annotations

import json
from io import StringIO
import sys

from src.analysis.trader_registry import save_wallet_report
from src.intelligence.wallet_discovery import init_wallet_discovery_db
from src.intelligence.wallet_performance import init_wallet_performance_db

WALLET = "0x" + "a" * 40


def _report():
    return {
        "wallet": WALLET,
        "classification": "market_maker",
        "confidence": 0.85,
        "watch_score": 75,
        "metrics": {
            "trade_count": 50,
            "markets_traded": 20,
            "overlap_ratio": 0.3,
            "merge_count": 5,
            "redeem_count": 2,
            "buy_volume": 1000,
            "sell_volume": 400,
            "btc_volume": 800,
            "eth_volume": 200,
            "sol_volume": 0,
        },
        "signals": [],
    }


def test_wallet_performance_cli_json(tmp_path, monkeypatch):
    traders_db = tmp_path / "traders.db"
    discovery_db = tmp_path / "discovery.db"
    init_wallet_discovery_db(traders_db, discovery_db)
    init_wallet_performance_db(traders_db)
    save_wallet_report(_report(), db_path=str(traders_db))

    monkeypatch.chdir(tmp_path)
    import src.intelligence.wallet_performance as perf_module

    monkeypatch.setattr(perf_module, "DEFAULT_TRADERS_DB", str(traders_db))
    monkeypatch.setattr(perf_module, "DEFAULT_TRADER_DISCOVERY_DB", str(discovery_db))

    from src.cli import wallet_performance_cli

    captured = StringIO()
    monkeypatch.setattr(sys, "stdout", captured)
    result = wallet_performance_cli(as_json=True, wallet=WALLET)
    assert "scores" in result
    payload = json.loads(captured.getvalue())
    assert payload["read_only"] is True


def test_wallet_performance_report_cli_human(tmp_path, monkeypatch):
    traders_db = tmp_path / "traders.db"
    discovery_db = tmp_path / "discovery.db"
    init_wallet_performance_db(traders_db)

    monkeypatch.chdir(tmp_path)
    import src.intelligence.wallet_performance_analytics as analytics_module

    monkeypatch.setattr(analytics_module, "DEFAULT_TRADERS_DB", str(traders_db))
    monkeypatch.setattr(analytics_module, "DEFAULT_TRADER_DISCOVERY_DB", str(discovery_db))

    from src.cli import wallet_performance_report_cli

    captured = StringIO()
    monkeypatch.setattr(sys, "stdout", captured)
    wallet_performance_report_cli(as_json=False, limit=5)
    assert "Tracked wallets" in captured.getvalue()


def test_wallet_feedback_cycle_cli(tmp_path, monkeypatch):
    traders_db = tmp_path / "traders.db"
    discovery_db = tmp_path / "discovery.db"
    init_wallet_discovery_db(traders_db, discovery_db)
    init_wallet_performance_db(traders_db)
    save_wallet_report(_report(), db_path=str(traders_db))

    monkeypatch.chdir(tmp_path)
    import src.intelligence.wallet_feedback_engine as feedback_module

    monkeypatch.setattr(feedback_module, "DEFAULT_TRADERS_DB", str(traders_db))
    monkeypatch.setattr(feedback_module, "DEFAULT_TRADER_DISCOVERY_DB", str(discovery_db))

    from src.cli import wallet_feedback_cycle_cli

    result = wallet_feedback_cycle_cli(as_json=True, limit=5)
    assert result["read_only"] is True
    assert "evaluated" in result
