from __future__ import annotations

import json

from src.analysis.trader_registry import save_wallet_report
from src.intelligence.wallet_discovery import WalletDiscoveryEngine, init_wallet_discovery_db
from src.intelligence.wallet_feedback_engine import FeedbackConfig, WalletFeedbackEngine
from src.intelligence.wallet_performance import WalletPerformanceEngine, init_wallet_performance_db
from src.intelligence.wallet_performance_analytics import wallet_performance_analytics_report

WALLET_A = "0x" + "a" * 40
WALLET_B = "0x" + "b" * 40


def _report(wallet: str, watch_score: int = 85):
    return {
        "wallet": wallet,
        "classification": "arbitrage_trader",
        "confidence": 0.9,
        "watch_score": watch_score,
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


def test_wallet_performance_score_structure(tmp_path):
    traders_db = tmp_path / "traders.db"
    discovery_db = tmp_path / "discovery.db"
    save_wallet_report(_report(WALLET_A), db_path=str(traders_db))
    init_wallet_performance_db(traders_db)

    engine = WalletPerformanceEngine(traders_db_path=traders_db, discovery_db_path=discovery_db)
    score = engine.score_wallet(WALLET_A)

    assert score.wallet == WALLET_A
    assert 0 <= score.score <= 100
    assert score.status in {"promoted", "active", "probation", "retired"}
    assert score.trend in {"improving", "declining", "stable"}
    assert "realized_roi" in score.components or "win_rate" in score.components


def test_persist_snapshot_and_actions(tmp_path):
    traders_db = tmp_path / "traders.db"
    discovery_db = tmp_path / "discovery.db"
    save_wallet_report(_report(WALLET_A), db_path=str(traders_db))
    init_wallet_performance_db(traders_db)

    engine = WalletPerformanceEngine(traders_db_path=traders_db, discovery_db_path=discovery_db)
    performance = engine.score_wallet(WALLET_A)
    engine.persist_snapshot(performance)
    engine.log_action(
        wallet=WALLET_A,
        action="promoted",
        previous_status="active",
        new_status="promoted",
        score=performance.score,
        reason="test promotion",
    )

    snapshots = engine.load_snapshots(wallet=WALLET_A, limit=5)
    actions = engine.load_actions(action="promoted", limit=5)
    assert len(snapshots) == 1
    assert snapshots[0]["wallet"] == WALLET_A
    assert len(actions) == 1
    assert actions[0]["action"] == "promoted"


def test_feedback_engine_promotion_and_retirement(tmp_path):
    traders_db = tmp_path / "traders.db"
    discovery_db = tmp_path / "discovery.db"
    init_wallet_discovery_db(traders_db, discovery_db)
    init_wallet_performance_db(traders_db)

    discovery = WalletDiscoveryEngine(
        traders_db_path=traders_db,
        discovery_db_path=discovery_db,
        wallet_export_dir=tmp_path / "wallets",
        watchlist_path=tmp_path / "watchlist.json",
    )
    save_wallet_report(_report(WALLET_A, watch_score=95), db_path=str(traders_db))
    save_wallet_report(_report(WALLET_B, watch_score=10), db_path=str(traders_db))

    config = FeedbackConfig(
        promote_score_threshold=50.0,
        retire_score_threshold=5.0,
        demote_score_threshold=20.0,
        min_confidence_for_promote=0.1,
        min_win_rate_for_promote=0.0,
    )
    feedback = WalletFeedbackEngine(
        config=config,
        traders_db_path=str(traders_db),
        discovery_db_path=str(discovery_db),
    )
    result = feedback.evaluate_all([WALLET_A, WALLET_B])
    assert result["evaluated"] == 2
    assert result["read_only"] is True


def test_performance_analytics_report(tmp_path):
    traders_db = tmp_path / "traders.db"
    discovery_db = tmp_path / "discovery.db"
    init_wallet_performance_db(traders_db)
    save_wallet_report(_report(WALLET_A), db_path=str(traders_db))

    engine = WalletPerformanceEngine(traders_db_path=traders_db, discovery_db_path=discovery_db)
    engine.persist_snapshot(engine.score_wallet(WALLET_A))

    report = wallet_performance_analytics_report(traders_db_path=traders_db, discovery_db_path=discovery_db)
    assert report["read_only"] is True
    assert "roi_distribution" in report
    assert "expectancy_distribution" in report
    assert "score_drift" in report
    assert "signal_decay" in report
