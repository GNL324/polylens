from __future__ import annotations

import json

from src.analysis.trader_registry import save_wallet_report
from src.intelligence.wallet_autonomy_service import CYCLE_NAMES, WalletAutonomyService
from src.intelligence.wallet_data_acquisition import (
    AcquisitionSourceConfig,
    AcquiredWallet,
    WalletDataAcquisitionEngine,
    init_wallet_acquisition_db,
)
from src.intelligence.wallet_acquisition_analytics import wallet_acquisition_analytics_report
from src.intelligence.wallet_quality_filter import WalletQualityFilter

WALLET = "0x" + "a1" * 20
WALLET_BAD = "not-a-wallet"


def _report():
    return {
        "wallet": WALLET,
        "classification": "arbitrage_trader",
        "confidence": 0.85,
        "watch_score": 80,
        "metrics": {"trade_count": 50, "markets_traded": 20, "overlap_ratio": 0.3},
        "signals": [],
    }


def test_discover_candidates_from_registry(tmp_path):
    traders_db = tmp_path / "traders.db"
    discovery_db = tmp_path / "discovery.db"
    save_wallet_report(_report(), db_path=str(traders_db))
    engine = WalletDataAcquisitionEngine(
        traders_db_path=traders_db,
        discovery_db_path=discovery_db,
        sources=[AcquisitionSourceConfig(name="registry", limit=10)],
        rate_limit_seconds=0.0,
        cache_ttl_seconds=0.0,
    )
    candidates = engine.discover_candidates(limit=10)
    assert any(row.wallet == WALLET for row in candidates)


def test_quality_filter_rejects_malformed():
    filt = WalletQualityFilter()
    score = filt.score_wallet(WALLET_BAD, {}, valid=False, reason="malformed")
    assert score.status == "rejected"
    assert score.quality_score == 0.0


def test_acquire_wallet_accepts_registry_wallet(tmp_path):
    traders_db = tmp_path / "traders.db"
    discovery_db = tmp_path / "discovery.db"
    save_wallet_report(_report(), db_path=str(traders_db))
    engine = WalletDataAcquisitionEngine(
        traders_db_path=traders_db,
        discovery_db_path=discovery_db,
        rate_limit_seconds=0.0,
        cache_ttl_seconds=0.0,
    )
    acquired = AcquiredWallet(wallet=WALLET, source="registry", confidence=0.85, evidence_count=50)
    result = engine.acquire_wallet(acquired)
    assert result["status"] in {"accepted", "probation"}
    records = engine.load_recent_records(limit=5)
    assert len(records) >= 1


def test_run_acquisition_persists_run(tmp_path):
    traders_db = tmp_path / "traders.db"
    discovery_db = tmp_path / "discovery.db"
    watchlist = tmp_path / "watchlist.json"
    watchlist.write_text(json.dumps([WALLET]), encoding="utf-8")
    save_wallet_report(_report(), db_path=str(traders_db))
    init_wallet_acquisition_db(traders_db, discovery_db)
    engine = WalletDataAcquisitionEngine(
        traders_db_path=traders_db,
        discovery_db_path=discovery_db,
        watchlist_path=watchlist,
        rate_limit_seconds=0.0,
        cache_ttl_seconds=0.0,
    )
    result = engine.run_acquisition(limit=10)
    assert result["read_only"] is True
    assert result["discovered"] >= 1


def test_acquisition_analytics_report(tmp_path):
    traders_db = tmp_path / "traders.db"
    discovery_db = tmp_path / "discovery.db"
    init_wallet_acquisition_db(traders_db, discovery_db)
    report = wallet_acquisition_analytics_report(traders_db_path=traders_db, discovery_db_path=discovery_db)
    assert report["read_only"] is True
    assert "registry_growth" in report
    assert "quality_score_distribution" in report


def test_autonomy_acquisition_cycle(tmp_path):
    traders_db = tmp_path / "traders.db"
    discovery_db = tmp_path / "discovery.db"
    save_wallet_report(_report(), db_path=str(traders_db))
    service = WalletAutonomyService(traders_db_path=traders_db, discovery_db_path=discovery_db)
    result = service.run_acquisition_cycle()
    assert result["cycle"] == "acquisition"
    assert result["status"] == "success"
    assert "acquisition" in CYCLE_NAMES
