from __future__ import annotations

import json

from src.analysis.trader_registry import save_wallet_report
from src.intelligence.wallet_autonomy_service import (
    CYCLE_NAMES,
    WalletAutonomyService,
    init_wallet_service_state_db,
    wallet_autonomy_report,
)
from src.intelligence.wallet_service_health import wallet_service_health_summary

WALLET = "0x" + "a" * 40


def _report():
    return {
        "wallet": WALLET,
        "classification": "arbitrage_trader",
        "confidence": 0.9,
        "watch_score": 85,
        "metrics": {
            "trade_count": 80,
            "markets_traded": 30,
            "overlap_ratio": 0.35,
            "merge_count": 8,
            "redeem_count": 4,
            "buy_volume": 2500,
            "sell_volume": 800,
            "btc_volume": 2000,
            "eth_volume": 500,
            "sol_volume": 0,
        },
        "signals": [],
    }


def test_init_wallet_service_state_db(tmp_path):
    traders_db = tmp_path / "traders.db"
    init_wallet_service_state_db(traders_db)
    service = WalletAutonomyService(traders_db_path=traders_db, discovery_db_path=tmp_path / "discovery.db")
    states = service.load_cycle_state()
    names = {row["cycle_name"] for row in states}
    assert "discovery" in names
    assert "__service__" in names


def test_cycle_is_due_when_never_run(tmp_path):
    traders_db = tmp_path / "traders.db"
    service = WalletAutonomyService(traders_db_path=traders_db, discovery_db_path=tmp_path / "discovery.db")
    assert service.cycle_is_due("signals") is True


def test_run_performance_cycle_persists_state(tmp_path):
    traders_db = tmp_path / "traders.db"
    discovery_db = tmp_path / "discovery.db"
    save_wallet_report(_report(), db_path=str(traders_db))
    service = WalletAutonomyService(traders_db_path=traders_db, discovery_db_path=discovery_db)
    result = service.run_performance_cycle()
    assert result["status"] == "success"
    states = service.load_cycle_state("performance")
    assert states[0]["last_status"] == "success"


def test_run_due_cycles_records_service_state(tmp_path):
    traders_db = tmp_path / "traders.db"
    discovery_db = tmp_path / "discovery.db"
    service = WalletAutonomyService(traders_db_path=traders_db, discovery_db_path=discovery_db)
    payload = service.run_due_cycles(force=True)
    assert payload["cycles_run"] == len(CYCLE_NAMES)
    service_state = service.load_cycle_state("__service__")
    assert service_state[0]["last_status"] in {"success", "partial"}


def test_wallet_autonomy_report_persisted(tmp_path):
    traders_db = tmp_path / "traders.db"
    discovery_db = tmp_path / "discovery.db"
    init_wallet_service_state_db(traders_db)
    report = wallet_autonomy_report(
        traders_db_path=traders_db,
        discovery_db_path=discovery_db,
        persist=True,
    )
    assert report["read_only"] is True
    assert "discovered_wallets" in report
    assert "performance_summary" in report


def test_wallet_service_health_summary(tmp_path):
    traders_db = tmp_path / "traders.db"
    discovery_db = tmp_path / "discovery.db"
    service = WalletAutonomyService(traders_db_path=traders_db, discovery_db_path=discovery_db)
    service.run_performance_cycle()
    health = wallet_service_health_summary(traders_db_path=str(traders_db), service=service)
    assert health["read_only"] is True
    assert "success_rate" in health
    assert len(health["cycles"]) == len(CYCLE_NAMES)
