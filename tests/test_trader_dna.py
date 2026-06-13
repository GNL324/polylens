from __future__ import annotations

import json
import sys
from pathlib import Path

from src.analysis.trader_discovery import TraderDiscoveryCandidate, save_discovery_candidate, save_discovery_relationships
from src.analysis.trader_dna import (
    build_trader_dna_report,
    build_trader_dna_vector,
    compute_similarity,
    dna_clusters,
    most_similar_traders,
)
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
        "sell_volume": 1200,
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


def _raw_event(
    wallet: str,
    *,
    timestamp: float,
    action: str = "buy",
    side: str = "up",
    title: str = "Bitcoin Up or Down - 5 minute",
    slug: str = "bitcoin-up-or-down-5m",
    amount: float = 10.0,
    tx_hash: str | None = None,
) -> dict:
    return {
        "wallet": wallet,
        "timestamp": timestamp,
        "event_type": action,
        "market_id": f"market-{wallet[-4:]}-{int(timestamp)}",
        "condition_id": f"cond-{wallet[-4:]}-{int(timestamp)}",
        "market_slug": slug,
        "market_title": title,
        "asset": "BTC" if "Bitcoin" in title else "ETH",
        "side": side,
        "action": action,
        "shares": 20.0,
        "price": 0.5,
        "amount": amount,
        "tx_hash": tx_hash or f"0x{wallet[-6:]}-{int(timestamp)}-{action}",
        "raw": {},
    }


def _write_export(export_dir: Path, wallet: str, events: list[dict]) -> None:
    export_dir.mkdir(parents=True, exist_ok=True)
    (export_dir / f"{wallet}_activity.json").write_text(
        json.dumps(
            {
                "wallet": wallet,
                "export_timestamp": "2026-06-12T00:00:00Z",
                "source": "test",
                "event_count": len(events),
                "events": events,
            }
        ),
        encoding="utf-8",
    )


def _fixture_events(wallet: str, *, hour_offset: int = 0) -> list[dict]:
    base = 1_718_000_000.0 + (hour_offset * 3_600)
    return [
        _raw_event(wallet, timestamp=base, action="buy", side="up", amount=12.0, tx_hash=f"{wallet}-1"),
        _raw_event(wallet, timestamp=base + 300, action="buy", side="down", amount=8.0, tx_hash=f"{wallet}-2"),
        _raw_event(wallet, timestamp=base + 600, action="merge", side="", amount=0.0, tx_hash=f"{wallet}-3"),
        _raw_event(wallet, timestamp=base + 900, action="redeem", side="", amount=15.0, tx_hash=f"{wallet}-4"),
        _raw_event(
            wallet,
            timestamp=base + 5_400,
            action="buy",
            side="up",
            title="Ethereum Up or Down - 15 minute",
            slug="ethereum-up-or-down-15m",
            amount=25.0,
            tx_hash=f"{wallet}-5",
        ),
    ]


def _seed_dna_fixtures(tmp_path: Path):
    traders_db = tmp_path / "traders.db"
    discovery_db = tmp_path / "discovery.db"
    export_dir = tmp_path / "wallets"
    export_dir.mkdir()

    save_wallet_report(_report(WALLET_SOURCE), watch_score=90, db_path=str(traders_db))
    save_wallet_report(_report(WALLET_A, overlap_count=180, overlap_ratio=0.36), watch_score=88, db_path=str(traders_db))
    save_wallet_report(
        _report(
            WALLET_B,
            overlap_count=175,
            overlap_ratio=0.35,
            merge_count=11,
            redeem_count=8,
            btc_volume=8200,
            eth_volume=450,
        ),
        watch_score=86,
        db_path=str(traders_db),
    )
    save_wallet_report(
        _report(
            WALLET_C,
            classification="quantitative_directional",
            confidence=0.7,
            trade_count=20,
            markets_traded=8,
            overlap_count=4,
            overlap_ratio=0.05,
            merge_count=0,
            redeem_count=0,
            buy_volume=400,
            sell_volume=350,
            btc_volume=200,
            eth_volume=100,
            sol_volume=50,
        ),
        watch_score=45,
        db_path=str(traders_db),
    )

    save_discovery_candidate(TraderDiscoveryCandidate(WALLET_A, "co_market_activity", 90, 186), db_path=discovery_db)
    save_discovery_candidate(TraderDiscoveryCandidate(WALLET_B, "co_market_activity", 88, 175), db_path=discovery_db)
    save_discovery_candidate(TraderDiscoveryCandidate(WALLET_C, "co_market_activity", 30, 4), db_path=discovery_db)
    save_discovery_relationships(
        WALLET_SOURCE,
        [
            TraderDiscoveryCandidate(WALLET_A, "co_market_activity", 90, 186, ["market-1", "market-2"]),
            TraderDiscoveryCandidate(WALLET_B, "co_market_activity", 88, 175, ["market-1", "market-2"]),
            TraderDiscoveryCandidate(WALLET_C, "co_market_activity", 30, 4, ["market-9"]),
        ],
        db_path=discovery_db,
    )

    _write_export(export_dir, WALLET_SOURCE, _fixture_events(WALLET_SOURCE))
    _write_export(export_dir, WALLET_A, _fixture_events(WALLET_A))
    _write_export(export_dir, WALLET_B, _fixture_events(WALLET_B, hour_offset=1))
    _write_export(export_dir, WALLET_C, _fixture_events(WALLET_C, hour_offset=12))

    return traders_db, discovery_db, export_dir


def test_build_trader_dna_vector_contains_expected_features(tmp_path):
    traders_db, discovery_db, export_dir = _seed_dna_fixtures(tmp_path)

    vector = build_trader_dna_vector(
        WALLET_SOURCE,
        traders_db_path=traders_db,
        discovery_db_path=discovery_db,
        wallet_export_dir=export_dir,
    )

    assert vector.wallet == WALLET_SOURCE
    assert vector.features["asset_btc"] > vector.features["asset_eth"]
    assert vector.features["merge_ratio"] > 0
    assert vector.features["alpha_norm"] > 0
    assert vector.metadata["specialization"] == "btc specialist"
    assert "BTC" in vector.metadata["preferred_assets"]


def test_similar_wallets_score_higher_than_dissimilar(tmp_path):
    traders_db, discovery_db, export_dir = _seed_dna_fixtures(tmp_path)

    focus = build_trader_dna_vector(
        WALLET_SOURCE,
        traders_db_path=traders_db,
        discovery_db_path=discovery_db,
        wallet_export_dir=export_dir,
    )
    similar = build_trader_dna_vector(
        WALLET_A,
        traders_db_path=traders_db,
        discovery_db_path=discovery_db,
        wallet_export_dir=export_dir,
    )
    dissimilar = build_trader_dna_vector(
        WALLET_C,
        traders_db_path=traders_db,
        discovery_db_path=discovery_db,
        wallet_export_dir=export_dir,
    )

    similar_score, _ = compute_similarity(focus, similar)
    dissimilar_score, _ = compute_similarity(focus, dissimilar)

    assert similar_score > dissimilar_score
    assert similar_score >= 70


def test_most_similar_traders_returns_ranked_matches(tmp_path):
    traders_db, discovery_db, export_dir = _seed_dna_fixtures(tmp_path)

    matches = most_similar_traders(
        WALLET_SOURCE,
        limit=3,
        traders_db_path=traders_db,
        discovery_db_path=discovery_db,
        wallet_export_dir=export_dir,
    )

    assert matches
    assert matches[0].wallet != WALLET_SOURCE
    assert matches[0].similarity_score >= matches[-1].similarity_score
    assert matches[0].shared_traits


def test_build_trader_dna_report_shape(tmp_path):
    traders_db, discovery_db, export_dir = _seed_dna_fixtures(tmp_path)

    report = build_trader_dna_report(
        WALLET_SOURCE,
        limit=2,
        traders_db_path=traders_db,
        discovery_db_path=discovery_db,
        wallet_export_dir=export_dir,
    )

    assert report["wallet"] == WALLET_SOURCE
    assert report["dna_vector"]["wallet"] == WALLET_SOURCE
    assert len(report["top_matches"]) == 2
    assert "similarity_score" in report["top_matches"][0]
    assert "shared_traits" in report["top_matches"][0]


def test_dna_clusters_finds_families(tmp_path):
    traders_db, discovery_db, export_dir = _seed_dna_fixtures(tmp_path)

    clusters = dna_clusters(
        [WALLET_SOURCE, WALLET_A, WALLET_B, WALLET_C],
        min_similarity=60.0,
        traders_db_path=traders_db,
        discovery_db_path=discovery_db,
        wallet_export_dir=export_dir,
    )

    assert clusters["cluster_count"] >= 1
    assert clusters["clusters"][0]["member_count"] >= 2
    assert clusters["clusters"][0]["family_label"]


def test_shared_traits_include_specialization_and_timing(tmp_path):
    traders_db, discovery_db, export_dir = _seed_dna_fixtures(tmp_path)

    matches = most_similar_traders(
        WALLET_SOURCE,
        limit=5,
        traders_db_path=traders_db,
        discovery_db_path=discovery_db,
        wallet_export_dir=export_dir,
    )
    top = next(match for match in matches if match.wallet == WALLET_A)

    assert any("btc specialist" in trait for trait in top.shared_traits)


def test_trader_dna_report_cli_json_output(tmp_path, capsys, monkeypatch):
    traders_db, discovery_db, export_dir = _seed_dna_fixtures(tmp_path)
    monkeypatch.setattr(
        "src.cli.build_trader_dna_report",
        lambda wallet, **kwargs: build_trader_dna_report(
            wallet,
            limit=kwargs.get("limit", 25),
            traders_db_path=traders_db,
            discovery_db_path=discovery_db,
            wallet_export_dir=export_dir,
        ),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["polylens", "trader-dna-report", "--wallet", WALLET_SOURCE, "--limit", "2"],
    )

    from src.cli import main

    main()
    payload = json.loads(capsys.readouterr().out)
    assert payload["wallet"] == WALLET_SOURCE
    assert len(payload["top_matches"]) == 2


def test_category_scores_present_in_matches(tmp_path):
    traders_db, discovery_db, export_dir = _seed_dna_fixtures(tmp_path)

    matches = most_similar_traders(
        WALLET_SOURCE,
        limit=1,
        traders_db_path=traders_db,
        discovery_db_path=discovery_db,
        wallet_export_dir=export_dir,
    )

    assert matches[0].category_scores
    assert "behavior" in matches[0].category_scores
    assert "market_participation" in matches[0].category_scores


def test_preferred_assets_from_breakdown_uses_asset_volumes():
    from src.analysis.trader_dna import _preferred_assets_from_breakdown

    assets = _preferred_assets_from_breakdown({"BTC": 8500, "ETH": 500, "SOL": 0, "OTHER": 10})

    assert assets == ["BTC", "ETH"]


def test_build_trader_dna_vector_falls_back_to_asset_breakdown_without_replay(tmp_path):
    traders_db, discovery_db, export_dir = _seed_dna_fixtures(tmp_path)
    export_path = export_dir / f"{WALLET_SOURCE}_activity.json"
    export_path.unlink()

    vector = build_trader_dna_vector(
        WALLET_SOURCE,
        traders_db_path=traders_db,
        discovery_db_path=discovery_db,
        wallet_export_dir=export_dir,
    )

    assert vector.metadata["preferred_assets"] == ["BTC", "ETH"]
