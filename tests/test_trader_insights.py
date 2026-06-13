from __future__ import annotations

import json
import sys
from pathlib import Path

from src.analysis.trader_discovery import TraderDiscoveryCandidate, save_discovery_candidate, save_discovery_relationships
from src.analysis.trader_insights import build_trader_insight_report, recommend_traders
from src.analysis.trader_network import TraderNetwork, TraderNode
from src.analysis.trader_registry import save_wallet_report

WALLET_A = "0x" + "a" * 40
WALLET_B = "0x" + "b" * 40
WALLET_C = "0x" + "c" * 40
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


def _seed_insight_fixtures(tmp_path: Path):
    traders_db = tmp_path / "traders.db"
    discovery_db = tmp_path / "discovery.db"
    export_dir = tmp_path / "wallets"
    export_dir.mkdir()

    save_wallet_report(_report(WALLET_A), watch_score=88, db_path=str(traders_db))
    save_wallet_report(
        _report(
            WALLET_B,
            classification="quantitative_directional",
            confidence=0.75,
            trade_count=40,
            markets_traded=8,
            overlap_count=12,
            overlap_ratio=0.05,
            merge_count=0,
            redeem_count=0,
            btc_volume=900,
            eth_volume=100,
        ),
        watch_score=55,
        db_path=str(traders_db),
    )
    save_wallet_report(
        _report(
            WALLET_C,
            classification="market_maker",
            confidence=0.85,
            trade_count=220,
            markets_traded=60,
            overlap_count=20,
            overlap_ratio=0.3,
            merge_count=80,
            redeem_count=60,
            btc_volume=5000,
            eth_volume=2000,
            sol_volume=1000,
        ),
        watch_score=72,
        db_path=str(traders_db),
    )
    save_discovery_candidate(TraderDiscoveryCandidate(WALLET_A, "co_market_activity", 90, 186), db_path=discovery_db)
    save_discovery_candidate(TraderDiscoveryCandidate(WALLET_B, "co_market_activity", 70, 12), db_path=discovery_db)
    save_discovery_candidate(TraderDiscoveryCandidate(WALLET_C, "co_market_activity", 65, 20), db_path=discovery_db)
    save_discovery_relationships(
        WALLET_SOURCE,
        [
            TraderDiscoveryCandidate(WALLET_A, "co_market_activity", 90, 186, ["market-1", "market-2"]),
            TraderDiscoveryCandidate(WALLET_B, "co_market_activity", 70, 12, ["market-3"]),
            TraderDiscoveryCandidate(WALLET_C, "co_market_activity", 65, 20, ["market-1", "market-4"]),
        ],
        db_path=discovery_db,
    )
    return traders_db, discovery_db, export_dir


def test_trader_insight_report_sections(tmp_path):
    traders_db, discovery_db, export_dir = _seed_insight_fixtures(tmp_path)

    report = build_trader_insight_report(
        WALLET_SOURCE,
        limit=3,
        traders_db_path=traders_db,
        discovery_db_path=discovery_db,
        wallet_export_dir=export_dir,
    )

    assert report["seed_wallet"] == WALLET_SOURCE
    assert report["summary"]["wallets_analyzed"] >= 3
    assert report["summary"]["edges"] > 0
    assert report["summary"]["clusters"] >= 1
    assert report["top_alpha_traders"]
    assert report["top_connected_traders"]
    assert report["top_btc_specialists"]
    assert report["top_directional_traders"]
    assert report["top_market_makers"]
    assert report["strongest_shared_market_relationships"]
    assert report["recommended_traders"]


def test_recommendations_include_reasons_and_exclude_seed(tmp_path):
    traders_db, discovery_db, export_dir = _seed_insight_fixtures(tmp_path)

    report = build_trader_insight_report(
        WALLET_SOURCE,
        limit=3,
        traders_db_path=traders_db,
        discovery_db_path=discovery_db,
        wallet_export_dir=export_dir,
    )

    assert all(item["wallet"] != WALLET_SOURCE for item in report["recommended_traders"])
    assert all(item["reason"] for item in report["recommended_traders"])
    assert report["recommended_traders"][0]["alpha_score"] >= report["recommended_traders"][-1]["alpha_score"]


def test_btc_specialist_recommendation_reason(tmp_path):
    traders_db, discovery_db, export_dir = _seed_insight_fixtures(tmp_path)
    report = build_trader_insight_report(
        WALLET_SOURCE,
        limit=5,
        traders_db_path=traders_db,
        discovery_db_path=discovery_db,
        wallet_export_dir=export_dir,
    )

    btc_rows = [row for row in report["recommended_traders"] if row["profile"] == "btc specialist"]
    assert btc_rows
    assert "BTC" in btc_rows[0]["reason"] or "co-market" in btc_rows[0]["reason"].lower()


def test_strongest_relationships_include_seed_edges(tmp_path):
    traders_db, discovery_db, export_dir = _seed_insight_fixtures(tmp_path)
    report = build_trader_insight_report(
        WALLET_SOURCE,
        limit=3,
        traders_db_path=traders_db,
        discovery_db_path=discovery_db,
        wallet_export_dir=export_dir,
    )

    relationships = report["strongest_shared_market_relationships"]
    assert relationships
    assert any(WALLET_SOURCE in {row["wallet_a"], row["wallet_b"]} for row in relationships)
    assert relationships[0]["shared_markets"] >= relationships[-1]["shared_markets"]


def test_recommend_traders_unit():
    profiles = {
        WALLET_A: {
            "wallet": WALLET_A,
            "profile": "btc specialist",
            "alpha_score": 88,
            "watch_score": 80,
            "shared_markets": 156,
        }
    }
    network = TraderNetwork(
        nodes={
            WALLET_A: TraderNode(WALLET_A, "market_maker", 80, 88, weighted_degree=250.0, degree=3),
        }
    )
    recommendations = recommend_traders(profiles, network=network, seed_wallet=WALLET_SOURCE, limit=1)
    assert recommendations == [
        {
            "wallet": WALLET_A,
            "profile": "btc specialist",
            "alpha_score": 88,
            "watch_score": 80,
            "shared_markets": 156,
            "reason": "High BTC specialization, strong co-market evidence, high alpha study value, central network position",
        }
    ]


def test_trader_insight_cli_json_output(capsys, monkeypatch):
    from src.cli import main

    expected = {
        "seed_wallet": WALLET_SOURCE,
        "summary": {"wallets_analyzed": 4, "edges": 3, "clusters": 1},
        "recommended_traders": [
            {
                "wallet": WALLET_A,
                "profile": "btc specialist",
                "alpha_score": 88,
                "shared_markets": 156,
                "reason": "High BTC specialization and strong co-market evidence",
            }
        ],
    }

    monkeypatch.setattr("src.cli.build_trader_insight_report", lambda seed_wallet, **kwargs: expected)
    sys.argv = ["polylens", "trader-insight-report", "--wallet", WALLET_SOURCE, "--limit", "5", "--json"]
    main()
    output = json.loads(capsys.readouterr().out)
    assert output == expected
