from __future__ import annotations

import json
import sys
from pathlib import Path

from src.analysis.trader_discovery import TraderDiscoveryCandidate, save_discovery_candidate, save_discovery_relationships
from src.analysis.trader_families import (
    FAMILY_TYPES,
    TraderFamily,
    build_trader_family_report,
    discover_trader_families,
    get_trader_family,
)
from src.analysis.trader_registry import save_wallet_report

WALLET_A = "0x" + "a" * 40
WALLET_B = "0x" + "b" * 40
WALLET_C = "0x" + "c" * 40
WALLET_D = "0x" + "d" * 40
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
    title: str = "Bitcoin Up or Down - 5 minute",
    slug: str = "bitcoin-up-or-down-5m",
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
        "side": "up",
        "action": action,
        "shares": 20.0,
        "price": 0.5,
        "amount": 10.0,
        "tx_hash": tx_hash or f"0x{wallet[-6:]}-{int(timestamp)}",
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
        _raw_event(wallet, timestamp=base, action="buy", tx_hash=f"{wallet}-1"),
        _raw_event(wallet, timestamp=base + 300, action="merge", tx_hash=f"{wallet}-2"),
        _raw_event(wallet, timestamp=base + 900, action="redeem", tx_hash=f"{wallet}-3"),
        _raw_event(
            wallet,
            timestamp=base + 5_400,
            action="buy",
            title="Ethereum Up or Down - 15 minute",
            slug="ethereum-up-or-down-15m",
            tx_hash=f"{wallet}-4",
        ),
    ]


def _seed_family_fixtures(tmp_path: Path):
    traders_db = tmp_path / "traders.db"
    discovery_db = tmp_path / "discovery.db"
    export_dir = tmp_path / "wallets"
    export_dir.mkdir()

    save_wallet_report(_report(WALLET_SOURCE), watch_score=90, db_path=str(traders_db))
    save_wallet_report(_report(WALLET_A, overlap_count=180, overlap_ratio=0.36), watch_score=88, db_path=str(traders_db))
    save_wallet_report(
        _report(WALLET_B, overlap_count=175, overlap_ratio=0.35, merge_count=11, redeem_count=8),
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
    save_wallet_report(
        _report(
            WALLET_D,
            classification="arbitrage_trader",
            confidence=0.82,
            trade_count=90,
            markets_traded=120,
            overlap_count=40,
            overlap_ratio=0.33,
            merge_count=2,
            redeem_count=1,
            btc_volume=1200,
            eth_volume=1100,
            sol_volume=200,
        ),
        watch_score=72,
        db_path=str(traders_db),
    )

    save_discovery_candidate(TraderDiscoveryCandidate(WALLET_A, "co_market_activity", 90, 186), db_path=discovery_db)
    save_discovery_candidate(TraderDiscoveryCandidate(WALLET_B, "co_market_activity", 88, 175), db_path=discovery_db)
    save_discovery_candidate(TraderDiscoveryCandidate(WALLET_C, "co_market_activity", 30, 4), db_path=discovery_db)
    save_discovery_candidate(TraderDiscoveryCandidate(WALLET_D, "co_market_activity", 70, 40), db_path=discovery_db)
    save_discovery_relationships(
        WALLET_SOURCE,
        [
            TraderDiscoveryCandidate(WALLET_A, "co_market_activity", 90, 186, ["market-1", "market-2"]),
            TraderDiscoveryCandidate(WALLET_B, "co_market_activity", 88, 175, ["market-1", "market-2"]),
            TraderDiscoveryCandidate(WALLET_C, "co_market_activity", 30, 4, ["market-9"]),
        ],
        db_path=discovery_db,
    )
    save_discovery_relationships(
        WALLET_D,
        [
            TraderDiscoveryCandidate(WALLET_C, "co_market_activity", 30, 4, ["market-9"]),
        ],
        db_path=discovery_db,
    )

    for wallet in (WALLET_SOURCE, WALLET_A, WALLET_B, WALLET_C, WALLET_D):
        _write_export(export_dir, wallet, _fixture_events(wallet))

    return traders_db, discovery_db, export_dir


def test_discover_trader_families_groups_similar_wallets(tmp_path):
    traders_db, discovery_db, export_dir = _seed_family_fixtures(tmp_path)

    families = discover_trader_families(
        [WALLET_SOURCE, WALLET_A, WALLET_B, WALLET_C, WALLET_D],
        limit=10,
        traders_db_path=traders_db,
        discovery_db_path=discovery_db,
        wallet_export_dir=export_dir,
    )

    assert families
    largest = families[0]
    assert largest.size >= 2
    assert largest.family_id.startswith("family_")
    assert largest.dominant_specialization
    assert largest.family_type in FAMILY_TYPES
    assert largest.top_wallets
    assert largest.shared_traits


def test_largest_family_contains_btc_specialists(tmp_path):
    traders_db, discovery_db, export_dir = _seed_family_fixtures(tmp_path)

    families = discover_trader_families(
        [WALLET_SOURCE, WALLET_A, WALLET_B, WALLET_C, WALLET_D],
        limit=10,
        traders_db_path=traders_db,
        discovery_db_path=discovery_db,
        wallet_export_dir=export_dir,
    )
    largest = families[0]

    assert largest.size >= 3
    assert largest.family_type in {"BTC Specialists", "Market Makers", "Hybrid Groups"}
    assert set(largest.top_wallets) & {WALLET_SOURCE, WALLET_A, WALLET_B}


def test_global_family_report_sections(tmp_path):
    traders_db, discovery_db, export_dir = _seed_family_fixtures(tmp_path)

    report = build_trader_family_report(
        limit=10,
        traders_db_path=traders_db,
        discovery_db_path=discovery_db,
        wallet_export_dir=export_dir,
    )

    assert report["family_count"] >= 1
    assert report["largest_families"]
    assert report["highest_alpha_families"]
    assert report["highest_watch_score_families"]
    assert "family_id" in report["largest_families"][0]


def test_single_family_report_lookup(tmp_path):
    traders_db, discovery_db, export_dir = _seed_family_fixtures(tmp_path)

    global_report = build_trader_family_report(
        limit=10,
        traders_db_path=traders_db,
        discovery_db_path=discovery_db,
        wallet_export_dir=export_dir,
    )
    family_id = global_report["families"][0]["family_id"]

    single = build_trader_family_report(
        family_id=family_id,
        limit=10,
        traders_db_path=traders_db,
        discovery_db_path=discovery_db,
        wallet_export_dir=export_dir,
    )

    assert single["family_id"] == family_id
    assert single["size"] >= 1
    assert "shared_traits" in single


def test_get_trader_family_returns_none_for_missing(tmp_path):
    traders_db, discovery_db, export_dir = _seed_family_fixtures(tmp_path)

    missing = get_trader_family(
        "family_99",
        traders_db_path=traders_db,
        discovery_db_path=discovery_db,
        wallet_export_dir=export_dir,
    )

    assert missing is None


def test_trader_family_report_cli_global(tmp_path, capsys, monkeypatch):
    traders_db, discovery_db, export_dir = _seed_family_fixtures(tmp_path)
    monkeypatch.setattr(
        "src.cli.build_trader_family_report",
        lambda **kwargs: build_trader_family_report(
            family_id=kwargs.get("family_id"),
            limit=kwargs.get("limit", 25),
            traders_db_path=traders_db,
            discovery_db_path=discovery_db,
            wallet_export_dir=export_dir,
        ),
    )
    monkeypatch.setattr(sys, "argv", ["polylens", "trader-family-report", "--limit", "5"])

    from src.cli import main

    main()
    payload = json.loads(capsys.readouterr().out)
    assert payload["family_count"] >= 1
    assert payload["largest_families"]


def test_trader_family_report_cli_single_family(tmp_path, capsys, monkeypatch):
    traders_db, discovery_db, export_dir = _seed_family_fixtures(tmp_path)
    families = discover_trader_families(
        [WALLET_SOURCE, WALLET_A, WALLET_B, WALLET_C, WALLET_D],
        traders_db_path=traders_db,
        discovery_db_path=discovery_db,
        wallet_export_dir=export_dir,
    )
    family_id = families[0].family_id
    monkeypatch.setattr(
        "src.cli.build_trader_family_report",
        lambda **kwargs: build_trader_family_report(
            family_id=kwargs.get("family_id"),
            limit=kwargs.get("limit", 25),
            traders_db_path=traders_db,
            discovery_db_path=discovery_db,
            wallet_export_dir=export_dir,
        ),
    )
    monkeypatch.setattr(sys, "argv", ["polylens", "trader-family-report", "--family", family_id])

    from src.cli import main

    main()
    payload = json.loads(capsys.readouterr().out)
    assert payload["family_id"] == family_id


def test_trader_family_to_dict_omits_members(tmp_path):
    traders_db, discovery_db, export_dir = _seed_family_fixtures(tmp_path)
    families = discover_trader_families(
        [WALLET_SOURCE, WALLET_A],
        traders_db_path=traders_db,
        discovery_db_path=discovery_db,
        wallet_export_dir=export_dir,
    )
    payload = families[0].to_dict()
    assert "members" not in payload
    assert payload["family_id"]


def test_build_trader_family_report_without_activity_exports(tmp_path):
    traders_db = tmp_path / "traders.db"
    discovery_db = tmp_path / "discovery.db"
    export_dir = tmp_path / "wallets"
    export_dir.mkdir()

    save_wallet_report(_report(WALLET_SOURCE), watch_score=90, db_path=str(traders_db))
    save_wallet_report(_report(WALLET_A, overlap_count=180), watch_score=88, db_path=str(traders_db))
    save_discovery_candidate(TraderDiscoveryCandidate(WALLET_A, "co_market_activity", 90, 186), db_path=discovery_db)
    save_discovery_relationships(
        WALLET_SOURCE,
        [TraderDiscoveryCandidate(WALLET_A, "co_market_activity", 90, 186, ["market-1"])],
        db_path=discovery_db,
    )

    report = build_trader_family_report(
        limit=5,
        traders_db_path=traders_db,
        discovery_db_path=discovery_db,
        wallet_export_dir=export_dir,
    )

    assert report["family_count"] >= 1
    assert report["largest_families"][0]["shared_traits"]
