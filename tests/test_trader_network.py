from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

from src.analysis.trader_discovery import TraderDiscoveryCandidate, save_discovery_candidate, save_discovery_relationships
from src.analysis.trader_network import (
    TraderEdge,
    TraderNetwork,
    TraderNode,
    build_trader_network,
    network_summary,
    top_bridge_wallets,
    top_connected_wallets,
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
        "overlap_markets": [],
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


def _activity_export(wallet: str, events: list[dict]) -> dict:
    return {"wallet": wallet, "events": events}


def _event(wallet: str, market: str):
    return {
        "market_id": market,
        "condition_id": market,
        "market_slug": f"{market}-slug",
        "raw": {"maker": wallet},
    }


def _seed_network_fixtures(tmp_path: Path):
    traders_db = tmp_path / "traders.db"
    discovery_db = tmp_path / "discovery.db"
    export_dir = tmp_path / "wallets"
    export_dir.mkdir()

    save_wallet_report(
        _report(WALLET_A, overlap_markets=["market-1", "market-2", "market-3"]),
        watch_score=92,
        db_path=str(traders_db),
    )
    save_wallet_report(
        _report(WALLET_B, overlap_markets=["market-1", "market-2"]),
        watch_score=80,
        db_path=str(traders_db),
    )
    save_wallet_report(
        _report(WALLET_C, classification="unknown", confidence=0.2, overlap_markets=["market-3"]),
        watch_score=10,
        db_path=str(traders_db),
    )
    save_wallet_report(
        _report(WALLET_D, overlap_markets=["market-4"]),
        watch_score=55,
        db_path=str(traders_db),
    )

    save_discovery_candidate(
        TraderDiscoveryCandidate(WALLET_A, "co_market_activity", 90, 3, ["market-1", "market-2"]),
        db_path=discovery_db,
    )
    save_discovery_candidate(
        TraderDiscoveryCandidate(WALLET_B, "co_market_activity", 75, 2, ["market-1", "market-2"]),
        db_path=discovery_db,
    )
    save_discovery_relationships(
        WALLET_SOURCE,
        [
            TraderDiscoveryCandidate(WALLET_A, "co_market_activity", 90, 186, ["market-1", "market-2", "market-3"]),
            TraderDiscoveryCandidate(WALLET_B, "co_market_activity", 75, 42, ["market-1", "market-2"]),
            TraderDiscoveryCandidate(WALLET_C, "co_market_activity", 40, 7, ["market-3"]),
        ],
        db_path=discovery_db,
    )

    (export_dir / f"{WALLET_SOURCE}_activity.json").write_text(
        json.dumps(
            _activity_export(
                WALLET_SOURCE,
                [
                    _event(WALLET_A, "market-1"),
                    _event(WALLET_B, "market-1"),
                    _event(WALLET_B, "market-2"),
                    _event(WALLET_C, "market-3"),
                ],
            )
        ),
        encoding="utf-8",
    )

    return traders_db, discovery_db, export_dir


def test_discovered_wallets_become_nodes_from_relationships(tmp_path):
    traders_db, discovery_db, export_dir = _seed_network_fixtures(tmp_path)

    network = build_trader_network(
        wallet=WALLET_SOURCE,
        traders_db_path=traders_db,
        discovery_db_path=discovery_db,
        wallet_export_dir=export_dir,
    )

    assert len(network.nodes) > 1
    assert WALLET_SOURCE in network.nodes
    assert WALLET_A in network.nodes
    assert WALLET_B in network.nodes


def test_network_handles_activity_db_without_wallet_events(tmp_path):
    traders_db, discovery_db, export_dir = _seed_network_fixtures(tmp_path)
    activity_db = tmp_path / "activity.db"
    sqlite3.connect(activity_db).close()

    network = build_trader_network(
        wallet=WALLET_SOURCE,
        traders_db_path=traders_db,
        discovery_db_path=discovery_db,
        wallet_export_dir=export_dir,
        wallet_activity_db_path=activity_db,
    )

    assert WALLET_SOURCE in network.nodes
    assert len(network.edges) > 0


def test_network_uses_wallet_events_when_table_exists(tmp_path):
    traders_db = tmp_path / "traders.db"
    discovery_db = tmp_path / "discovery.db"
    export_dir = tmp_path / "wallets"
    export_dir.mkdir()
    activity_db = tmp_path / "activity.db"
    save_wallet_report(_report(WALLET_A), db_path=str(traders_db))
    save_wallet_report(_report(WALLET_B), db_path=str(traders_db))
    with sqlite3.connect(activity_db) as conn:
        conn.execute(
            "CREATE TABLE wallet_events (wallet TEXT, condition_id TEXT, market_id TEXT, market_slug TEXT)"
        )
        conn.execute(
            "INSERT INTO wallet_events (wallet, condition_id, market_id, market_slug) VALUES (?, ?, ?, ?)",
            (WALLET_A, "market-shared", "", ""),
        )
        conn.execute(
            "INSERT INTO wallet_events (wallet, condition_id, market_id, market_slug) VALUES (?, ?, ?, ?)",
            (WALLET_B, "market-shared", "", ""),
        )

    network = build_trader_network(
        traders_db_path=traders_db,
        discovery_db_path=discovery_db,
        wallet_export_dir=export_dir,
        wallet_activity_db_path=activity_db,
    )

    edge_lookup = {(edge.wallet_a, edge.wallet_b): edge for edge in network.edges}
    ab_edge = edge_lookup.get((WALLET_A, WALLET_B)) or edge_lookup.get((WALLET_B, WALLET_A))
    assert ab_edge is not None
    assert ab_edge.shared_markets == ["market-shared"]


def test_shared_market_relationships_become_weighted_edges(tmp_path):
    traders_db, discovery_db, export_dir = _seed_network_fixtures(tmp_path)

    network = build_trader_network(
        wallet=WALLET_SOURCE,
        traders_db_path=traders_db,
        discovery_db_path=discovery_db,
        wallet_export_dir=export_dir,
    )

    edge_lookup = {(edge.wallet_a, edge.wallet_b): edge for edge in network.edges}
    source_a = edge_lookup.get((WALLET_SOURCE, WALLET_A)) or edge_lookup.get((WALLET_A, WALLET_SOURCE))
    assert source_a is not None
    assert source_a.weight == 186
    assert len(network.edges) > 0


def test_network_report_contains_multiple_nodes(tmp_path):
    traders_db, discovery_db, export_dir = _seed_network_fixtures(tmp_path)

    network = build_trader_network(
        wallet=WALLET_SOURCE,
        traders_db_path=traders_db,
        discovery_db_path=discovery_db,
        wallet_export_dir=export_dir,
    )
    summary = network_summary(network, limit=5, focus_wallet=WALLET_SOURCE)

    assert summary["nodes"] > 1
    assert summary["edges"] > 0
    assert summary["top_connected_wallets"]
    assert summary["top_bridge_wallets"]
    assert summary["focus_neighbors"]


def test_graph_creation_and_edge_weighting(tmp_path):
    traders_db, discovery_db, export_dir = _seed_network_fixtures(tmp_path)

    network = build_trader_network(
        traders_db_path=traders_db,
        discovery_db_path=discovery_db,
        wallet_export_dir=export_dir,
    )

    assert len(network.nodes) >= 4
    assert len(network.edges) >= 2

    edge_lookup = {(edge.wallet_a, edge.wallet_b): edge for edge in network.edges}
    ab_edge = edge_lookup.get((WALLET_A, WALLET_B)) or edge_lookup.get((WALLET_B, WALLET_A))
    assert ab_edge is not None
    assert ab_edge.weight == 2
    assert set(ab_edge.shared_markets) == {"market-1", "market-2"}


def test_centrality_and_cluster_detection(tmp_path):
    traders_db, discovery_db, export_dir = _seed_network_fixtures(tmp_path)

    network = build_trader_network(
        traders_db_path=traders_db,
        discovery_db_path=discovery_db,
        wallet_export_dir=export_dir,
    )

    node_a = network.nodes[WALLET_A]
    node_b = network.nodes[WALLET_B]
    assert node_a.degree >= node_b.degree
    assert node_a.centrality_score >= node_b.centrality_score
    assert node_a.cluster_id >= 0
    assert network.clusters >= 1


def test_bridge_wallet_prefers_cross_cluster_connector(tmp_path):
    traders_db, discovery_db, export_dir = _seed_network_fixtures(tmp_path)

    network = build_trader_network(
        traders_db_path=traders_db,
        discovery_db_path=discovery_db,
        wallet_export_dir=export_dir,
    )

    bridges = top_bridge_wallets(network, limit=3)
    assert bridges
    assert bridges[0]["wallet"] in {WALLET_SOURCE, WALLET_A, WALLET_B, WALLET_C}


def test_top_connected_wallets_sorted_by_weighted_degree(tmp_path):
    traders_db, discovery_db, export_dir = _seed_network_fixtures(tmp_path)

    network = build_trader_network(
        traders_db_path=traders_db,
        discovery_db_path=discovery_db,
        wallet_export_dir=export_dir,
    )

    connected = top_connected_wallets(network, limit=2)
    assert connected[0]["wallet"] == WALLET_SOURCE
    assert connected[0]["weighted_degree"] >= connected[1]["weighted_degree"]


def test_network_summary_shape(tmp_path):
    traders_db, discovery_db, export_dir = _seed_network_fixtures(tmp_path)

    network = build_trader_network(
        traders_db_path=traders_db,
        discovery_db_path=discovery_db,
        wallet_export_dir=export_dir,
    )
    summary = network_summary(network, limit=3, focus_wallet=WALLET_A)

    assert summary["nodes"] == len(network.nodes)
    assert summary["edges"] == len(network.edges)
    assert summary["clusters"] == network.clusters
    assert len(summary["top_connected_wallets"]) == 3
    assert len(summary["top_bridge_wallets"]) == 3
    assert summary["focus_wallet"]["wallet"] == WALLET_A
    assert summary["focus_neighbors"]


def test_ego_network_depth_limits_nodes(tmp_path):
    traders_db, discovery_db, export_dir = _seed_network_fixtures(tmp_path)

    full_network = build_trader_network(
        traders_db_path=traders_db,
        discovery_db_path=discovery_db,
        wallet_export_dir=export_dir,
    )
    shallow_network = build_trader_network(
        wallet=WALLET_A,
        depth=1,
        traders_db_path=traders_db,
        discovery_db_path=discovery_db,
        wallet_export_dir=export_dir,
    )

    assert len(shallow_network.nodes) <= len(full_network.nodes)
    assert WALLET_A in shallow_network.nodes


def test_trader_network_cli_json_output(capsys, monkeypatch, tmp_path):
    from src.cli import main

    traders_db, discovery_db, export_dir = _seed_network_fixtures(tmp_path)
    expected = {
        "nodes": 4,
        "edges": 2,
        "clusters": 2,
        "top_connected_wallets": [{"wallet": WALLET_A}],
        "top_bridge_wallets": [{"wallet": WALLET_B}],
    }

    def fake_build_trader_network(**kwargs):
        assert kwargs.get("wallet") == WALLET_A
        assert kwargs.get("depth") == 2
        return TraderNetwork(
            nodes={
                WALLET_A: TraderNode(WALLET_A, "market_maker", 90, 88, degree=2, weighted_degree=4.0, centrality_score=0.8),
                WALLET_B: TraderNode(WALLET_B, "market_maker", 70, 60, degree=1, weighted_degree=2.0, centrality_score=0.4),
            },
            edges=[TraderEdge(WALLET_A, WALLET_B, ["market-1"], 1)],
            clusters=2,
        )

    def fake_network_summary(network, *, limit=25, focus_wallet=None):
        assert focus_wallet == WALLET_A
        assert limit == 5
        return expected

    monkeypatch.setattr("src.cli.build_trader_network", fake_build_trader_network)
    monkeypatch.setattr("src.cli.network_summary", fake_network_summary)
    sys.argv = [
        "polylens",
        "trader-network-report",
        "--wallet",
        WALLET_A,
        "--depth",
        "2",
        "--limit",
        "5",
        "--json",
    ]
    main()
    output = json.loads(capsys.readouterr().out)
    assert output == expected
