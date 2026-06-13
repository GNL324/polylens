from __future__ import annotations

import json
import sys

from src.analysis.trader_alpha import calculate_alpha_score, rank_trader_alpha, trader_alpha_report
from src.analysis.trader_discovery import TraderDiscoveryCandidate, save_discovery_candidate
from src.analysis.trader_registry import save_wallet_report

WALLET_A = "0x" + "a" * 40
WALLET_B = "0x" + "b" * 40


def _report(wallet: str, classification: str = "market_maker", confidence: float = 0.9, **metrics):
    base_metrics = {
        "trade_count": 120,
        "markets_traded": 18,
        "overlap_count": 7,
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
        "watch_score": 92,
        "metrics": base_metrics,
        "signals": [{"name": "high_merge_frequency"}, {"name": "high_overlap_frequency"}],
    }


def test_alpha_score_bounds():
    score = calculate_alpha_score(
        activity_count=10_000,
        markets_traded=500,
        shared_markets=500,
        overlap_ratio=1.0,
        merge_count=500,
        redeem_count=500,
        buy_volume=1_000_000,
        redeem_volume=1_000_000,
        asset_breakdown={"BTC": 1_000_000},
        classification="market_maker",
        confidence=1.0,
        watch_score=999,
    )
    assert score == 100


def test_unknown_traders_score_low():
    score = calculate_alpha_score(
        activity_count=2,
        markets_traded=1,
        shared_markets=0,
        overlap_ratio=0,
        merge_count=0,
        redeem_count=0,
        buy_volume=5,
        redeem_volume=0,
        asset_breakdown={"OTHER": 5},
        classification="unknown",
        confidence=0.1,
        watch_score=0,
    )
    assert score <= 10


def test_high_evidence_market_maker_scores_high(tmp_path):
    traders_db = tmp_path / "traders.db"
    discovery_db = tmp_path / "discovery.db"
    save_wallet_report(_report(WALLET_A), watch_score=92, db_path=str(traders_db))
    save_discovery_candidate(TraderDiscoveryCandidate(WALLET_A, "co_market_activity", 90, 12, ["m1", "m2"]), db_path=discovery_db)

    report = trader_alpha_report(WALLET_A, traders_db_path=traders_db, discovery_db_path=discovery_db)

    assert report["alpha_score"] >= 80
    assert report["shared_markets"] == 12
    assert report["asset_breakdown"]["BTC"] == 8500


def test_low_sample_traders_are_penalized(tmp_path):
    traders_db = tmp_path / "traders.db"
    save_wallet_report(
        _report(
            WALLET_A,
            classification="quantitative_directional",
            confidence=0.45,
            trade_count=2,
            markets_traded=1,
            overlap_count=0,
            overlap_ratio=0,
            merge_count=0,
            redeem_count=0,
            buy_volume=10,
            redeem_volume=0,
            btc_volume=10,
            eth_volume=0,
        ),
        watch_score=20,
        db_path=str(traders_db),
    )

    report = trader_alpha_report(WALLET_A, traders_db_path=traders_db, discovery_db_path=tmp_path / "discovery.db")

    assert report["activity_count"] == 2
    assert report["alpha_score"] < 35


def test_rank_trader_alpha_orders_by_alpha_score(tmp_path):
    traders_db = tmp_path / "traders.db"
    discovery_db = tmp_path / "discovery.db"
    save_wallet_report(_report(WALLET_A), watch_score=92, db_path=str(traders_db))
    save_wallet_report(_report(WALLET_B, "unknown", 0.1, trade_count=1, buy_volume=5, markets_traded=1, overlap_count=0), watch_score=0, db_path=str(traders_db))
    save_discovery_candidate(TraderDiscoveryCandidate(WALLET_A, "co_market_activity", 90, 8), db_path=discovery_db)

    ranked = rank_trader_alpha(limit=2, traders_db_path=traders_db, discovery_db_path=discovery_db)

    assert [row["wallet"] for row in ranked] == [WALLET_A, WALLET_B]
    assert ranked[0]["alpha_score"] > ranked[1]["alpha_score"]


def test_trader_alpha_cli_json_output(capsys, monkeypatch):
    from src.cli import main

    expected = {"wallet": WALLET_A, "alpha_score": 88}

    def fake_report(wallet):
        assert wallet == WALLET_A
        return expected

    monkeypatch.setattr("src.cli.trader_alpha_report", fake_report)
    sys.argv = ["polylens", "trader-alpha-report", "--wallet", WALLET_A, "--json"]
    main()
    output = json.loads(capsys.readouterr().out)
    assert output == expected
