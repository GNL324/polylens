from __future__ import annotations

import json

from src.analysis.trader_discovery import TraderDiscoveryCandidate, save_discovery_candidate
from src.analysis.trader_registry import save_wallet_report
from src.intelligence.wallet_discovery import (
    DiscoveryDbSource,
    RegistryDiscoverySource,
    WalletDiscoveryConfig,
    WalletDiscoveryEngine,
    init_wallet_discovery_db,
)
from src.intelligence.wallet_discovery_analytics import wallet_discovery_analytics_report
from src.intelligence.wallet_scoring import WalletScorer

WALLET_A = "0x" + "a" * 40
WALLET_B = "0x" + "b" * 40


def _report(wallet: str, classification: str = "arbitrage_trader"):
    return {
        "wallet": wallet,
        "classification": classification,
        "confidence": 0.9,
        "watch_score": 85,
        "metrics": {
            "trade_count": 80,
            "markets_traded": 30,
            "overlap_count": 12,
            "overlap_ratio": 0.35,
            "merge_count": 8,
            "redeem_count": 4,
            "buy_volume": 2500,
            "sell_volume": 800,
            "btc_volume": 2000,
            "eth_volume": 500,
            "sol_volume": 0,
            "arbitrage_opportunities": 5,
            "arbitrage_volume": 400,
            "average_sum_price": 0.96,
        },
        "signals": [],
    }


def test_registry_discovery_source(tmp_path):
    traders_db = tmp_path / "traders.db"
    save_wallet_report(_report(WALLET_A), db_path=str(traders_db))
    source = RegistryDiscoverySource(traders_db_path=traders_db)
    candidates = source.discover(limit=10)
    assert len(candidates) == 1
    assert candidates[0].wallet == WALLET_A


def test_discover_new_wallets_dedupes_and_persists(tmp_path):
    traders_db = tmp_path / "traders.db"
    discovery_db = tmp_path / "discovery.db"
    watchlist = tmp_path / "watchlist.json"
    watchlist.write_text(json.dumps([WALLET_A]), encoding="utf-8")

    save_wallet_report(_report(WALLET_B), db_path=str(traders_db))
    save_discovery_candidate(
        TraderDiscoveryCandidate(wallet=WALLET_A, source="test", discovery_score=40, evidence_count=2),
        db_path=discovery_db,
    )

    config = WalletDiscoveryConfig(rate_limit_seconds=0.0, cache_ttl_seconds=0.0)
    engine = WalletDiscoveryEngine(
        config=config,
        traders_db_path=traders_db,
        discovery_db_path=discovery_db,
        watchlist_path=watchlist,
        wallet_export_dir=tmp_path / "wallets",
    )
    result = engine.discover_new_wallets(limit=10)

    assert result["candidates_found"] >= 1
    wallets = set(result["wallets"])
    assert WALLET_A in wallets or WALLET_B in wallets


def test_score_and_rank_orders_wallets(tmp_path):
    traders_db = tmp_path / "traders.db"
    discovery_db = tmp_path / "discovery.db"
    save_wallet_report({**_report(WALLET_A), "watch_score": 90}, db_path=str(traders_db))
    save_wallet_report(_report(WALLET_B, classification="mixed"), db_path=str(traders_db))

    engine = WalletDiscoveryEngine(
        traders_db_path=traders_db,
        discovery_db_path=discovery_db,
        wallet_export_dir=tmp_path / "wallets",
        watchlist_path=tmp_path / "watchlist.json",
    )
    ranked = engine.score_and_rank([WALLET_A, WALLET_B])
    assert ranked[0].rank == 1
    assert ranked[0].score >= ranked[1].score


def test_lifecycle_promotion_and_retirement(tmp_path):
    traders_db = tmp_path / "traders.db"
    discovery_db = tmp_path / "discovery.db"
    engine = WalletDiscoveryEngine(
        traders_db_path=traders_db,
        discovery_db_path=discovery_db,
        wallet_export_dir=tmp_path / "wallets",
        watchlist_path=tmp_path / "watchlist.json",
    )
    scorer = WalletScorer(traders_db_path=traders_db, discovery_db_path=discovery_db)

    high = scorer.score_wallet(WALLET_A)
    high = type(high)(wallet=high.wallet, score=85.0, confidence=0.8, rank=1, category=high.category, components=high.components, metrics=high.metrics)
    low = scorer.score_wallet(WALLET_B)
    low = type(low)(wallet=low.wallet, score=15.0, confidence=0.2, rank=2, category=low.category, components=low.components, metrics=low.metrics)

    lifecycle = engine.apply_lifecycle([high, low])
    assert lifecycle["state_counts"].get("active", 0) >= 1
    assert lifecycle["state_counts"].get("retired", 0) >= 1

    init_wallet_discovery_db(traders_db, discovery_db)
    active = engine.load_lifecycle(state="active", limit=10)
    retired = engine.load_lifecycle(state="retired", limit=10)
    assert any(row["wallet"] == WALLET_A for row in active)
    assert any(row["wallet"] == WALLET_B for row in retired)


def test_discovery_analytics_report(tmp_path):
    traders_db = tmp_path / "traders.db"
    discovery_db = tmp_path / "discovery.db"
    init_wallet_discovery_db(traders_db, discovery_db)

    engine = WalletDiscoveryEngine(traders_db_path=traders_db, discovery_db_path=discovery_db)
    engine.discover_new_wallets(limit=5)

    report = wallet_discovery_analytics_report(traders_db_path=traders_db, discovery_db_path=discovery_db)
    assert report["read_only"] is True
    assert "discovery_runs" in report
    assert "score_distribution" in report
