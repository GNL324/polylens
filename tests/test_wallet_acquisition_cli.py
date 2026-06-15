from __future__ import annotations

import json
from io import StringIO
import sys

from src.analysis.trader_registry import save_wallet_report
from src.intelligence.wallet_data_acquisition import init_wallet_acquisition_db

WALLET = "0x" + "a" * 40


def _report():
    return {
        "wallet": WALLET,
        "classification": "market_maker",
        "confidence": 0.8,
        "watch_score": 70,
        "metrics": {"trade_count": 40, "markets_traded": 15},
        "signals": [],
    }


def test_wallet_acquire_cli_json(tmp_path, monkeypatch):
    traders_db = tmp_path / "traders.db"
    discovery_db = tmp_path / "discovery.db"
    init_wallet_acquisition_db(traders_db, discovery_db)
    save_wallet_report(_report(), db_path=str(traders_db))

    monkeypatch.chdir(tmp_path)
    import src.intelligence.wallet_data_acquisition as acquisition_module

    monkeypatch.setattr(acquisition_module, "DEFAULT_TRADERS_DB", str(traders_db))
    monkeypatch.setattr(acquisition_module, "DEFAULT_TRADER_DISCOVERY_DB", str(discovery_db))

    from src.cli import wallet_acquire_cli

    captured = StringIO()
    monkeypatch.setattr(sys, "stdout", captured)
    result = wallet_acquire_cli(as_json=True, limit=5)
    assert result["read_only"] is True
    payload = json.loads(captured.getvalue())
    assert "discovered" in payload


def test_wallet_acquisition_report_cli_human(tmp_path, monkeypatch):
    traders_db = tmp_path / "traders.db"
    init_wallet_acquisition_db(traders_db, tmp_path / "discovery.db")

    monkeypatch.chdir(tmp_path)
    import src.intelligence.wallet_acquisition_analytics as analytics_module

    monkeypatch.setattr(analytics_module, "DEFAULT_TRADERS_DB", str(traders_db))

    from src.cli import wallet_acquisition_report_cli

    captured = StringIO()
    monkeypatch.setattr(sys, "stdout", captured)
    wallet_acquisition_report_cli(as_json=False, days=7)
    assert "Accepted:" in captured.getvalue()


def test_wallet_registry_growth_cli(tmp_path, monkeypatch):
    traders_db = tmp_path / "traders.db"
    save_wallet_report(_report(), db_path=str(traders_db))

    monkeypatch.chdir(tmp_path)
    import src.analysis.trader_registry as registry_module
    import src.intelligence.wallet_acquisition_analytics as analytics_module

    monkeypatch.setattr(registry_module, "DEFAULT_TRADERS_DB", str(traders_db))
    monkeypatch.setattr(analytics_module, "DEFAULT_TRADERS_DB", str(traders_db))

    from src.cli import wallet_registry_growth_cli

    result = wallet_registry_growth_cli(as_json=True, limit=5)
    assert result["registry_count"] >= 1
