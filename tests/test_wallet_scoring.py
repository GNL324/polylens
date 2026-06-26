from __future__ import annotations

import json

from src.analysis.trader_registry import save_wallet_report
from src.intelligence.wallet_scoring import WalletScorer

WALLET = "0x" + "a" * 40


def _report():
    return {
        "wallet": WALLET,
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
        "signals": [],
    }


def test_wallet_score_fields(tmp_path, signal_db_path):
    traders_db = tmp_path / "traders.db"
    save_wallet_report(_report(), db_path=str(traders_db))

    scorer = WalletScorer(
        traders_db_path=traders_db,
        discovery_db_path=tmp_path / "discovery.db",
        signal_db_path=signal_db_path,
        paper_copy_db_path=tmp_path / "paper_copy.db",
    )
    score = scorer.score_wallet(WALLET)

    assert score.wallet == WALLET
    assert 0 <= score.score <= 100
    assert 0 <= score.confidence <= 1
    assert score.category
    assert "roi" in score.components
    assert "win_rate" in score.components
    assert "signal_freshness" in score.components


def test_rank_wallets_assigns_ranks(tmp_path, signal_db_path):
    traders_db = tmp_path / "traders.db"
    wallet_b = "0x" + "b" * 40
    save_wallet_report(_report(), db_path=str(traders_db))
    save_wallet_report({**_report(), "wallet": wallet_b, "watch_score": 40}, db_path=str(traders_db))

    scorer = WalletScorer(
        traders_db_path=traders_db,
        discovery_db_path=tmp_path / "discovery.db",
        signal_db_path=signal_db_path,
        paper_copy_db_path=tmp_path / "paper_copy.db",
    )
    ranked = scorer.rank_wallets([WALLET, wallet_b])

    assert ranked[0].rank == 1
    assert ranked[1].rank == 2
    assert ranked[0].score >= ranked[1].score
