from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.analysis.trader_registry import save_wallet_report
from src.intelligence.real_wallet_ingestion import (
    load_real_seed_entries,
    real_wallet_inventory,
    run_real_wallet_ingestion,
)
from src.intelligence.wallet_alpha_lab import WalletAlphaLab
from src.intelligence.wallet_data_acquisition import WalletDataAcquisitionEngine
from src.intelligence.wallet_feedback_engine import WalletFeedbackEngine
from src.intelligence.wallet_quality_filter import WalletQualityFilter
from src.intelligence.wallet_synthetic_filter import (
    KNOWN_SYNTHETIC_WALLETS,
    filter_production_wallets,
    is_synthetic_wallet,
    partition_wallets,
)
from src.intelligence.wallet_tracker import WalletTracker

REAL_WALLET = "0x927f7694de44d19a72bce76254e628d1c141d215"
SYNTHETIC_A = "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
SYNTHETIC_B = "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
FIXTURES = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "wallet_forensics"


def _report(wallet: str, classification: str = "arbitrage_trader"):
    return {
        "wallet": wallet,
        "classification": classification,
        "confidence": 0.9,
        "watch_score": 85,
        "metrics": {"trade_count": 50, "markets_traded": 20},
        "signals": [],
    }


def test_known_synthetic_wallets_detected():
    assert is_synthetic_wallet(SYNTHETIC_A)
    assert is_synthetic_wallet(SYNTHETIC_B)
    assert not is_synthetic_wallet(REAL_WALLET)


def test_repeated_character_wallet_detected():
    from src.intelligence.wallet_synthetic_filter import is_synthetic_seed_wallet

    assert is_synthetic_seed_wallet("0xcccccccccccccccccccccccccccccccccccccccc")


def test_filter_production_wallets():
    wallets = [REAL_WALLET, SYNTHETIC_A, SYNTHETIC_B]
    filtered = filter_production_wallets(wallets)
    assert REAL_WALLET in filtered
    assert SYNTHETIC_A not in filtered
    assert SYNTHETIC_B not in filtered


def test_partition_wallets():
    real, synthetic = partition_wallets([REAL_WALLET, SYNTHETIC_A])
    assert REAL_WALLET in real
    assert SYNTHETIC_A in synthetic


def test_quality_filter_rejects_synthetic():
    filt = WalletQualityFilter()
    score = filt.score_wallet(SYNTHETIC_A, {"confidence": 0.9, "evidence_count": 50})
    assert score.status == "rejected"
    assert "synthetic wallet" in score.reason


def test_alpha_rankings_exclude_synthetic(tmp_path):
    traders_db = tmp_path / "traders.db"
    discovery_db = tmp_path / "discovery.db"
    save_wallet_report(_report(REAL_WALLET), db_path=str(traders_db))
    save_wallet_report(_report(SYNTHETIC_A), db_path=str(traders_db))
    lab = WalletAlphaLab(traders_db_path=traders_db, discovery_db_path=discovery_db)
    rankings = lab.rank_wallets(limit=10)
    wallets = {row.wallet for row in rankings}
    assert REAL_WALLET in wallets or len(wallets) <= 1
    assert SYNTHETIC_A not in wallets


def test_feedback_skips_synthetic_promotion(tmp_path):
    traders_db = tmp_path / "traders.db"
    discovery_db = tmp_path / "discovery.db"
    engine = WalletFeedbackEngine(traders_db_path=traders_db, discovery_db_path=discovery_db)
    result = engine.evaluate_wallet(SYNTHETIC_A)
    assert result.get("skipped") is True
    assert result.get("action") is None


def test_acquisition_rejects_synthetic(tmp_path):
    traders_db = tmp_path / "traders.db"
    discovery_db = tmp_path / "discovery.db"
    from src.intelligence.wallet_data_acquisition import AcquiredWallet

    engine = WalletDataAcquisitionEngine(
        traders_db_path=traders_db,
        discovery_db_path=discovery_db,
        rate_limit_seconds=0.0,
        cache_ttl_seconds=0.0,
    )
    result = engine.acquire_wallet(
        AcquiredWallet(wallet=SYNTHETIC_A, source="watchlist", confidence=0.9, evidence_count=50)
    )
    assert result["status"] == "rejected"
    assert "synthetic wallet" in result["reason"]


def test_watchlist_excludes_synthetic(tmp_path):
    traders_db = tmp_path / "traders.db"
    discovery_db = tmp_path / "discovery.db"
    watchlist = tmp_path / "watchlist.json"
    watchlist.write_text(json.dumps([REAL_WALLET, SYNTHETIC_A]), encoding="utf-8")
    save_wallet_report(_report(REAL_WALLET), db_path=str(traders_db))
    save_wallet_report(_report(SYNTHETIC_A), db_path=str(traders_db))
    tracker = WalletTracker(
        traders_db_path=traders_db,
        discovery_db_path=discovery_db,
        watchlist_path=watchlist,
    )
    entries = tracker.build_ranked_watchlist(limit=10)
    wallets = {entry.wallet for entry in entries}
    assert SYNTHETIC_A not in wallets


def test_real_seed_entries_exclude_synthetic():
    entries = load_real_seed_entries()
    assert len(entries) >= 1
    assert all(not is_synthetic_wallet(str(row.get("wallet") or "")) for row in entries)


def test_real_wallet_ingestion_populates_registry(tmp_path):
    traders_db = tmp_path / "traders.db"
    discovery_db = tmp_path / "discovery.db"
    export_dir = tmp_path / "wallets"
    export_dir.mkdir()
    seed_exports = tmp_path / "seed_exports"
    seed_exports.mkdir()
    for path in FIXTURES.glob("*.json"):
        if path.name == "market_maker_wallet.json":
            seed_exports.joinpath(path.name).write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    real_seed = tmp_path / "real_seed_wallets.json"
    real_seed.write_text(
        json.dumps(
            [
                {
                    "wallet": REAL_WALLET,
                    "source": "real_seed_export",
                    "confidence": 0.85,
                    "export_path": str(seed_exports / "market_maker_wallet.json"),
                }
            ]
        ),
        encoding="utf-8",
    )
    result = run_real_wallet_ingestion(
        traders_db_path=traders_db,
        discovery_db_path=discovery_db,
        real_seed_path=real_seed,
        export_dir=export_dir,
        limit=10,
    )
    inventory = result["inventory"]
    assert inventory["real_wallet_count"] > 0
    assert REAL_WALLET not in inventory.get("synthetic_wallets", [])


def test_synthetic_fixtures_allowed_in_tests():
    assert SYNTHETIC_A in KNOWN_SYNTHETIC_WALLETS
    fixture = FIXTURES / "arbitrage_wallet.json"
    assert fixture.exists()
    payload = json.loads(fixture.read_text(encoding="utf-8"))
    assert is_synthetic_wallet(payload["wallet"])
