from __future__ import annotations

import json
from io import StringIO
import sys

from src.analysis.trader_registry import save_wallet_report
from src.intelligence.wallet_alpha_lab import init_wallet_alpha_lab_db

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


def test_wallet_alpha_report_cli_wallet(tmp_path, monkeypatch):
    traders_db = tmp_path / "traders.db"
    discovery_db = tmp_path / "discovery.db"
    init_wallet_alpha_lab_db(traders_db, discovery_db)
    save_wallet_report(_report(), db_path=str(traders_db))

    monkeypatch.chdir(tmp_path)
    import src.intelligence.wallet_alpha_lab as lab_module

    monkeypatch.setattr(lab_module, "DEFAULT_TRADERS_DB", str(traders_db))
    monkeypatch.setattr(lab_module, "DEFAULT_TRADER_DISCOVERY_DB", str(discovery_db))

    from src.cli import wallet_alpha_report_cli

    result = wallet_alpha_report_cli(as_json=True, wallet=WALLET)
    assert result["read_only"] is True
    assert "report" in result


def test_wallet_alpha_rankings_cli(tmp_path, monkeypatch):
    traders_db = tmp_path / "traders.db"
    discovery_db = tmp_path / "discovery.db"
    save_wallet_report(_report(), db_path=str(traders_db))

    monkeypatch.chdir(tmp_path)
    import src.intelligence.wallet_alpha_lab as lab_module

    monkeypatch.setattr(lab_module, "DEFAULT_TRADERS_DB", str(traders_db))
    monkeypatch.setattr(lab_module, "DEFAULT_TRADER_DISCOVERY_DB", str(discovery_db))

    from src.cli import wallet_alpha_rankings_cli

    captured = StringIO()
    monkeypatch.setattr(sys, "stdout", captured)
    wallet_alpha_rankings_cli(as_json=False, limit=5)
    assert "grade=" in captured.getvalue() or captured.getvalue() == ""


def test_wallet_alpha_baselines_cli_json(tmp_path, monkeypatch):
    traders_db = tmp_path / "traders.db"
    discovery_db = tmp_path / "discovery.db"
    init_wallet_alpha_lab_db(traders_db, discovery_db)

    monkeypatch.chdir(tmp_path)
    import src.intelligence.wallet_baseline_analysis as baseline_module

    monkeypatch.setattr(baseline_module, "DEFAULT_TRADERS_DB", str(traders_db))
    monkeypatch.setattr(baseline_module, "DEFAULT_TRADER_DISCOVERY_DB", str(discovery_db))

    from src.cli import wallet_alpha_baselines_cli

    result = wallet_alpha_baselines_cli(as_json=True)
    assert result["read_only"] is True
    assert "baselines" in result
