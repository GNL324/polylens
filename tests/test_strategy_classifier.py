from __future__ import annotations

import json

import pytest

from src.analysis.trader_registry import save_wallet_report
from src.intelligence.strategy_classifier import (
    STRATEGY_ARCHETYPES,
    StrategyClassifier,
    StrategyProfile,
    init_strategy_profiles_db,
)

WALLET = "0x" + "a" * 40


def _arb_report(wallet: str = WALLET):
    return {
        "wallet": wallet,
        "classification": "arbitrage_trader",
        "confidence": 0.92,
        "watch_score": 88,
        "metrics": {
            "trade_count": 90,
            "markets_traded": 35,
            "overlap_count": 15,
            "overlap_ratio": 0.4,
            "merge_count": 10,
            "redeem_count": 3,
            "buy_volume": 3200,
            "sell_volume": 900,
            "btc_volume": 2500,
            "eth_volume": 700,
            "sol_volume": 0,
            "arbitrage_opportunities": 8,
            "arbitrage_volume": 600,
            "average_sum_price": 0.95,
        },
        "signals": [{"name": "high_overlap_frequency"}],
    }


def _scalper_report(wallet: str = WALLET):
    return {
        "wallet": wallet,
        "classification": "mixed",
        "confidence": 0.8,
        "watch_score": 60,
        "metrics": {
            "trade_count": 120,
            "markets_traded": 40,
            "overlap_count": 5,
            "overlap_ratio": 0.1,
            "merge_count": 1,
            "redeem_count": 2,
            "buy_volume": 600,
            "sell_volume": 550,
            "btc_volume": 400,
            "eth_volume": 200,
            "sol_volume": 0,
            "arbitrage_opportunities": 0,
            "arbitrage_volume": 0,
            "average_sum_price": 1.0,
        },
        "signals": [],
    }


def test_classify_wallet_arb_hunter(tmp_path):
    traders_db = tmp_path / "traders.db"
    save_wallet_report(_arb_report(), db_path=str(traders_db))

    classifier = StrategyClassifier(traders_db_path=traders_db)
    profile = classifier.classify_wallet(WALLET)

    assert isinstance(profile, StrategyProfile)
    assert profile.archetype == "ARB_HUNTER"
    assert profile.archetype in STRATEGY_ARCHETYPES
    assert profile.confidence > 0
    assert profile.forensics_classification == "arbitrage_trader"
    assert profile.alpha_score >= 0


def test_classify_wallet_size_scalper(tmp_path):
    traders_db = tmp_path / "traders.db"
    save_wallet_report(_scalper_report(), db_path=str(traders_db))

    classifier = StrategyClassifier(traders_db_path=traders_db)
    profile = classifier.classify_wallet(WALLET)

    assert profile.archetype == "SIZE_SCALPER"
    assert profile.supporting_metrics["trade_count"] == 120


def test_persist_and_load_profile(tmp_path):
    traders_db = tmp_path / "traders.db"
    save_wallet_report(_arb_report(), db_path=str(traders_db))

    classifier = StrategyClassifier(traders_db_path=traders_db)
    profile = classifier.classify_wallet(WALLET)
    classifier.persist_profile(profile)

    init_strategy_profiles_db(traders_db)
    loaded = classifier.load_profile(WALLET)
    assert loaded is not None
    assert loaded.archetype == profile.archetype
    assert loaded.wallet == WALLET


def test_profiles_by_archetype(tmp_path):
    traders_db = tmp_path / "traders.db"
    save_wallet_report(_arb_report(), db_path=str(traders_db))

    classifier = StrategyClassifier(traders_db_path=traders_db)
    profiles = classifier.classify_wallets([WALLET])
    classifier.persist_profiles(profiles)

    matches = classifier.profiles_by_archetype("ARB_HUNTER")
    assert len(matches) == 1
    assert matches[0].wallet == WALLET


def test_classify_wallet_requires_data(tmp_path):
    traders_db = tmp_path / "traders.db"
    classifier = StrategyClassifier(traders_db_path=traders_db)
    with pytest.raises(ValueError, match="no wallet report found"):
        classifier.classify_wallet(WALLET, scan_if_missing=True)
