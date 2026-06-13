from __future__ import annotations

import json
import sys

from src.analysis.trader_discovery import TraderDiscoveryCandidate, save_discovery_candidate, save_discovery_relationships
from src.analysis.trader_profiler import (
    derive_specialization,
    profile_traders,
    profile_wallet,
    select_profile_wallets,
)
from src.analysis.trader_registry import save_wallet_report

WALLET_A = "0x" + "a" * 40
WALLET_B = "0x" + "b" * 40
WALLET_SOURCE = "0x" + "9" * 40


def _report(wallet: str, classification: str = "market_maker", confidence: float = 0.9, **metrics):
    base_metrics = {
        "trade_count": 120,
        "markets_traded": 540,
        "overlap_count": 186,
        "overlap_ratio": 0.38,
        "merge_count": 12,
        "redeem_count": 9,
        "buy_volume": 7200,
        "redeem_volume": 1800,
        "btc_volume": 8500,
        "eth_volume": 500,
        "sol_volume": 0,
        "other_volume": 0,
    }
    base_metrics.update(metrics)
    return {
        "wallet": wallet,
        "classification": classification,
        "confidence": confidence,
        "watch_score": 88,
        "metrics": base_metrics,
        "signals": [{"name": "high_merge_frequency"}, {"name": "high_overlap_frequency"}],
    }


def test_derive_specialization_rules():
    assert derive_specialization(
        classification="market_maker",
        confidence=0.9,
        activity_count=140,
        markets_traded=18,
        shared_markets=7,
        overlap_ratio=0.38,
        merge_count=12,
        redeem_count=9,
        btc_volume=8500,
        eth_volume=500,
        sol_volume=0,
        other_volume=0,
    ) == "btc specialist"

    assert derive_specialization(
        classification="market_maker",
        confidence=0.9,
        activity_count=140,
        markets_traded=18,
        shared_markets=7,
        overlap_ratio=0.38,
        merge_count=4,
        redeem_count=3,
        btc_volume=4000,
        eth_volume=4000,
        sol_volume=500,
        other_volume=0,
    ) == "crypto market maker"

    assert derive_specialization(
        classification="arbitrage_trader",
        confidence=0.85,
        activity_count=80,
        markets_traded=40,
        shared_markets=186,
        overlap_ratio=0.42,
        merge_count=4,
        redeem_count=3,
        btc_volume=1200,
        eth_volume=900,
        sol_volume=100,
        other_volume=0,
    ) == "cross-market arbitrage"

    assert derive_specialization(
        classification="market_maker",
        confidence=0.9,
        activity_count=220,
        markets_traded=60,
        shared_markets=20,
        overlap_ratio=0.3,
        merge_count=80,
        redeem_count=60,
        btc_volume=5000,
        eth_volume=2000,
        sol_volume=1000,
        other_volume=0,
    ) == "high-frequency inventory manager"

    assert derive_specialization(
        classification="quantitative_directional",
        confidence=0.75,
        activity_count=40,
        markets_traded=8,
        shared_markets=1,
        overlap_ratio=0.05,
        merge_count=0,
        redeem_count=0,
        btc_volume=900,
        eth_volume=100,
        sol_volume=0,
        other_volume=0,
    ) == "directional trader"


def test_profile_wallet_from_registry(tmp_path):
    traders_db = tmp_path / "traders.db"
    discovery_db = tmp_path / "discovery.db"
    save_wallet_report(_report(WALLET_A), watch_score=88, db_path=str(traders_db))
    save_discovery_candidate(
        TraderDiscoveryCandidate(WALLET_A, "co_market_activity", 90, 186, ["market-1"]),
        db_path=discovery_db,
    )

    profile = profile_wallet(
        WALLET_A,
        scan_if_missing=False,
        traders_db_path=traders_db,
        discovery_db_path=discovery_db,
    )

    assert profile.wallet == WALLET_A
    assert profile.profile == "btc specialist"
    assert profile.specialization == profile.profile
    assert profile.alpha_score >= 80
    assert profile.watch_score == 88
    assert profile.markets_traded == 540
    assert profile.shared_markets == 186
    assert profile.btc_volume == 8500


def test_profile_traders_top_connected(tmp_path, monkeypatch):
    traders_db = tmp_path / "traders.db"
    discovery_db = tmp_path / "discovery.db"
    export_dir = tmp_path / "wallets"
    export_dir.mkdir()

    save_wallet_report(_report(WALLET_A), watch_score=88, db_path=str(traders_db))
    save_wallet_report(_report(WALLET_B, classification="arbitrage_trader"), watch_score=70, db_path=str(traders_db))
    save_discovery_relationships(
        WALLET_SOURCE,
        [
            TraderDiscoveryCandidate(WALLET_A, "co_market_activity", 90, 186, ["market-1"]),
            TraderDiscoveryCandidate(WALLET_B, "co_market_activity", 75, 42, ["market-1", "market-2"]),
        ],
        db_path=discovery_db,
    )

    profiles = profile_traders(
        top_connected=True,
        focus_wallet=WALLET_SOURCE,
        limit=2,
        scan_if_missing=False,
        traders_db_path=traders_db,
        discovery_db_path=discovery_db,
        wallet_export_dir=export_dir,
    )

    assert len(profiles) == 2
    assert profiles[0]["wallet"] in {WALLET_SOURCE, WALLET_A, WALLET_B}
    assert "profile" in profiles[0]
    assert "alpha_score" in profiles[0]


def test_profile_traders_top_alpha(tmp_path):
    traders_db = tmp_path / "traders.db"
    discovery_db = tmp_path / "discovery.db"
    save_wallet_report(_report(WALLET_A), watch_score=88, db_path=str(traders_db))
    save_wallet_report(_report(WALLET_B, classification="unknown", confidence=0.1, trade_count=1, buy_volume=5), watch_score=0, db_path=str(traders_db))

    profiles = profile_traders(
        top_alpha=True,
        limit=2,
        scan_if_missing=False,
        traders_db_path=traders_db,
        discovery_db_path=discovery_db,
    )

    assert profiles[0]["wallet"] == WALLET_A
    assert profiles[0]["alpha_score"] >= profiles[1]["alpha_score"]


def test_select_profile_wallets_top_connected_overrides_single_wallet(tmp_path):
    traders_db = tmp_path / "traders.db"
    discovery_db = tmp_path / "discovery.db"
    export_dir = tmp_path / "wallets"
    export_dir.mkdir()
    save_wallet_report(_report(WALLET_A), watch_score=88, db_path=str(traders_db))
    save_wallet_report(_report(WALLET_B, classification="arbitrage_trader"), watch_score=70, db_path=str(traders_db))
    save_discovery_relationships(
        WALLET_SOURCE,
        [
            TraderDiscoveryCandidate(WALLET_A, "co_market_activity", 90, 186, ["market-1"]),
            TraderDiscoveryCandidate(WALLET_B, "co_market_activity", 75, 42, ["market-1", "market-2"]),
        ],
        db_path=discovery_db,
    )

    wallets = select_profile_wallets(
        wallet=WALLET_A,
        top_connected=True,
        focus_wallet=WALLET_SOURCE,
        limit=3,
        traders_db_path=traders_db,
        discovery_db_path=discovery_db,
        wallet_export_dir=export_dir,
    )

    assert len(wallets) == 3
    assert WALLET_A in wallets


def test_select_profile_wallets_defaults_to_discovery(tmp_path):
    discovery_db = tmp_path / "discovery.db"
    save_discovery_candidate(TraderDiscoveryCandidate(WALLET_A, "co_market_activity", 90, 12), db_path=discovery_db)
    save_discovery_candidate(TraderDiscoveryCandidate(WALLET_B, "co_market_activity", 70, 8), db_path=discovery_db)

    wallets = select_profile_wallets(limit=2, discovery_db_path=discovery_db)
    assert wallets == [WALLET_A, WALLET_B]


def test_profile_traders_cli_json_output(capsys, monkeypatch):
    from src.cli import main

    expected = [
        {
            "wallet": WALLET_A,
            "profile": "btc specialist",
            "alpha_score": 91,
            "watch_score": 88,
            "markets_traded": 540,
            "shared_markets": 186,
        }
    ]

    monkeypatch.setattr("src.cli.profile_traders", lambda **kwargs: expected)
    sys.argv = ["polylens", "profile-traders", "--wallet", WALLET_A, "--json"]
    main()
    output = json.loads(capsys.readouterr().out)
    assert output == {"profiles": expected}
