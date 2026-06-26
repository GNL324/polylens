from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from src.analysis.trader_alpha import build_trader_alpha_report
from src.analysis.trader_discovery import (
    DEFAULT_TRADER_DISCOVERY_DB,
    DEFAULT_WALLET_EXPORT_DIR,
    TraderDiscoveryRelationship,
    discover_candidates_from_wallet,
    load_discovered_wallets,
    load_discovery_relationships,
)
from src.analysis.trader_registry import DEFAULT_TRADERS_DB, list_traders, load_wallet_report
from src.analysis.wallet_activity import DEFAULT_WALLET_ACTIVITY_DB
from src.sqlite_utils import closing_connection

DEFAULT_NETWORK_LIMIT = 10_000


@dataclass
class TraderNode:
    wallet: str
    classification: str
    watch_score: int
    alpha_score: int
    degree: int = 0
    weighted_degree: float = 0.0
    centrality_score: float = 0.0
    bridge_score: float = 0.0
    cluster_score: float = 0.0
    cluster_id: int = -1
    profitable_connection_score: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class TraderEdge:
    wallet_a: str
    wallet_b: str
    shared_markets: list[str] = field(default_factory=list)
    weight: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class TraderNetwork:
    nodes: dict[str, TraderNode] = field(default_factory=dict)
    edges: list[TraderEdge] = field(default_factory=list)
    clusters: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "nodes": len(self.nodes),
            "edges": len(self.edges),
            "clusters": self.clusters,
            "node_details": [node.to_dict() for node in sorted(self.nodes.values(), key=lambda item: item.wallet)],
            "edge_details": [edge.to_dict() for edge in self.edges],
        }


def build_trader_network(
    wallet: str | None = None,
    depth: int | None = None,
    traders_db_path: str | Path = DEFAULT_TRADERS_DB,
    discovery_db_path: str | Path = DEFAULT_TRADER_DISCOVERY_DB,
    wallet_export_dir: str | Path = DEFAULT_WALLET_EXPORT_DIR,
    wallet_activity_db_path: str | Path = DEFAULT_WALLET_ACTIVITY_DB,
) -> TraderNetwork:
    focus_wallet = str(wallet or "").strip().lower() or None

    wallet_markets, seed_wallets = _collect_wallet_markets(
        traders_db_path=traders_db_path,
        discovery_db_path=discovery_db_path,
        wallet_export_dir=wallet_export_dir,
        wallet_activity_db_path=wallet_activity_db_path,
        focus_wallet=focus_wallet,
    )

    relationships = _load_relationships(
        discovery_db_path=discovery_db_path,
        focus_wallet=focus_wallet,
        wallet_export_dir=wallet_export_dir,
    )
    relationship_edges = _edges_from_relationships(relationships)
    market_edges = _edges_from_markets(_build_edge_markets(wallet_markets))
    edges = _merge_edges(relationship_edges + market_edges)
    adjacency = _build_adjacency(edges)

    allowed_wallets = set(seed_wallets)
    allowed_wallets.update(wallet_markets)
    allowed_wallets.update(edge.wallet_a for edge in edges)
    allowed_wallets.update(edge.wallet_b for edge in edges)
    if focus_wallet:
        allowed_wallets.add(focus_wallet)

    if focus_wallet and depth is not None:
        if focus_wallet not in adjacency and focus_wallet in allowed_wallets:
            adjacency[focus_wallet] = {}
        allowed_wallets = _ego_network(focus_wallet, adjacency, max(0, int(depth)))

    edges = [
        edge
        for edge in edges
        if edge.wallet_a in allowed_wallets and edge.wallet_b in allowed_wallets
    ]
    wallet_markets = {key: value for key, value in wallet_markets.items() if key in allowed_wallets}
    adjacency = _build_adjacency(edges)

    nodes = _build_nodes(
        seed_wallets=allowed_wallets,
        wallet_markets=wallet_markets,
        edges=edges,
        adjacency=adjacency,
        traders_db_path=traders_db_path,
        discovery_db_path=discovery_db_path,
    )
    cluster_ids = _assign_clusters(nodes, edges)
    _apply_cluster_scores(nodes, edges, cluster_ids)
    _apply_bridge_scores(nodes, adjacency, cluster_ids)
    _apply_centrality_scores(nodes)
    _apply_profitable_connection_scores(nodes, adjacency)

    return TraderNetwork(
        nodes=nodes,
        edges=edges,
        clusters=len({cluster_id for cluster_id in cluster_ids.values() if cluster_id >= 0}),
    )


def network_summary(
    network: TraderNetwork,
    *,
    limit: int = 25,
    focus_wallet: str | None = None,
) -> dict[str, Any]:
    summary = {
        "nodes": len(network.nodes),
        "edges": len(network.edges),
        "clusters": network.clusters,
        "top_connected_wallets": top_connected_wallets(network, limit=limit),
        "top_bridge_wallets": top_bridge_wallets(network, limit=limit),
    }
    focus_wallet = str(focus_wallet or "").strip().lower() or None
    if focus_wallet and focus_wallet in network.nodes:
        summary["focus_wallet"] = network.nodes[focus_wallet].to_dict()
        summary["focus_neighbors"] = _neighbor_summary(network, focus_wallet, limit=limit)
    return summary


def top_connected_wallets(network: TraderNetwork, limit: int = 25) -> list[dict[str, Any]]:
    ordered = sorted(
        network.nodes.values(),
        key=lambda node: (-node.weighted_degree, -node.degree, -node.centrality_score, node.wallet),
    )
    return [_wallet_rank_entry(node) for node in ordered[: max(int(limit or 25), 0)]]


def top_bridge_wallets(network: TraderNetwork, limit: int = 25) -> list[dict[str, Any]]:
    ordered = sorted(
        network.nodes.values(),
        key=lambda node: (-node.bridge_score, -node.weighted_degree, -node.centrality_score, node.wallet),
    )
    return [_wallet_rank_entry(node) for node in ordered[: max(int(limit or 25), 0)]]


def _wallet_rank_entry(node: TraderNode) -> dict[str, Any]:
    return {
        "wallet": node.wallet,
        "classification": node.classification,
        "watch_score": node.watch_score,
        "alpha_score": node.alpha_score,
        "degree": node.degree,
        "weighted_degree": round(node.weighted_degree, 2),
        "centrality_score": round(node.centrality_score, 4),
        "bridge_score": round(node.bridge_score, 4),
        "cluster_score": round(node.cluster_score, 4),
        "cluster_id": node.cluster_id,
        "profitable_connection_score": round(node.profitable_connection_score, 2),
    }


def _neighbor_summary(network: TraderNetwork, wallet: str, limit: int = 25) -> list[dict[str, Any]]:
    neighbors: list[tuple[str, int]] = []
    for edge in network.edges:
        if edge.wallet_a == wallet:
            neighbors.append((edge.wallet_b, edge.weight))
        elif edge.wallet_b == wallet:
            neighbors.append((edge.wallet_a, edge.weight))
    neighbors.sort(key=lambda item: (-item[1], item[0]))
    results = []
    for neighbor_wallet, weight in neighbors[: max(int(limit or 25), 0)]:
        node = network.nodes.get(neighbor_wallet)
        if node is None:
            continue
        entry = _wallet_rank_entry(node)
        entry["shared_market_count"] = weight
        results.append(entry)
    return results


def _load_relationships(
    *,
    discovery_db_path: str | Path,
    focus_wallet: str | None,
    wallet_export_dir: str | Path,
) -> list[TraderDiscoveryRelationship]:
    if focus_wallet:
        relationships = load_discovery_relationships(source_wallet=focus_wallet, db_path=discovery_db_path)
        if not relationships:
            export_path = Path(wallet_export_dir) / f"{focus_wallet}_activity.json"
            if export_path.exists():
                discover_candidates_from_wallet(
                    focus_wallet,
                    output_dir=wallet_export_dir,
                    db_path=discovery_db_path,
                )
                relationships = load_discovery_relationships(source_wallet=focus_wallet, db_path=discovery_db_path)
        return relationships
    return load_discovery_relationships(db_path=discovery_db_path)


def _collect_wallet_markets(
    *,
    traders_db_path: str | Path,
    discovery_db_path: str | Path,
    wallet_export_dir: str | Path,
    wallet_activity_db_path: str | Path,
    focus_wallet: str | None,
) -> tuple[dict[str, set[str]], set[str]]:
    wallet_markets: dict[str, set[str]] = defaultdict(set)
    seed_wallets: set[str] = set()

    traders = list_traders(limit=DEFAULT_NETWORK_LIMIT, db_path=str(traders_db_path))
    for trader in traders:
        seed_wallets.add(trader.wallet)
        report = load_wallet_report(trader.wallet, db_path=str(traders_db_path))
        if report:
            metrics = report.to_dict()["report"].get("metrics") or {}
            for market in metrics.get("overlap_markets") or []:
                normalized = _normalize_market(market)
                if normalized:
                    wallet_markets[trader.wallet].add(normalized)

    try:
        discovered = load_discovered_wallets(discovery_db_path)
    except Exception:
        discovered = []
    for candidate in discovered:
        seed_wallets.add(candidate.wallet)
        for market in candidate.markets_seen:
            normalized = _normalize_market(market)
            if normalized:
                wallet_markets[candidate.wallet].add(normalized)

    relationships = load_discovery_relationships(
        source_wallet=focus_wallet,
        db_path=discovery_db_path,
    ) if focus_wallet else load_discovery_relationships(db_path=discovery_db_path)
    for relationship in relationships:
        seed_wallets.add(relationship.source_wallet)
        seed_wallets.add(relationship.target_wallet)
        for market in relationship.markets_seen:
            normalized = _normalize_market(market)
            if normalized:
                wallet_markets[relationship.source_wallet].add(normalized)
                wallet_markets[relationship.target_wallet].add(normalized)

    export_dir = Path(wallet_export_dir)
    if export_dir.exists():
        for path in sorted(export_dir.glob("*_activity.json")):
            wallet = path.name.replace("_activity.json", "").lower()
            seed_wallets.add(wallet)
            wallet_markets[wallet].update(_markets_from_activity_export(path))

    wallet_markets.update(_markets_from_activity_db(wallet_activity_db_path, wallets=seed_wallets))
    if focus_wallet:
        seed_wallets.add(focus_wallet)
        wallet_markets[focus_wallet].update(
            _markets_from_activity_db(wallet_activity_db_path, wallets={focus_wallet}).get(focus_wallet, set())
        )

    return dict(wallet_markets), seed_wallets


def _markets_from_activity_db(
    db_path: str | Path,
    wallets: set[str] | None = None,
) -> dict[str, set[str]]:
    path = Path(db_path)
    if not path.exists():
        return {}
    wallet_markets: dict[str, set[str]] = defaultdict(set)
    with closing_connection(path) as conn:
        table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='wallet_events'"
        ).fetchone()
        if table is None:
            return {}
        try:
            if wallets:
                placeholders = ",".join("?" for _ in wallets)
                rows = conn.execute(
                    f"""
                    SELECT wallet, condition_id, market_id, market_slug
                    FROM wallet_events
                    WHERE wallet IN ({placeholders})
                    """,
                    tuple(sorted(wallets)),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT wallet, condition_id, market_id, market_slug
                    FROM wallet_events
                    """
                ).fetchall()
        except sqlite3.OperationalError:
            return {}
    for row in rows:
        for value in (row["condition_id"], row["market_id"], row["market_slug"]):
            normalized = _normalize_market(value)
            if normalized:
                wallet_markets[row["wallet"]].add(normalized)
    return dict(wallet_markets)


def _markets_from_activity_export(path: str | Path) -> set[str]:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()
    markets: set[str] = set()
    for event in _extract_events(payload):
        if not isinstance(event, dict):
            continue
        market = _market_key(event)
        if market:
            markets.add(market)
    return markets


def _edges_from_relationships(relationships: list[TraderDiscoveryRelationship]) -> list[TraderEdge]:
    edges: list[TraderEdge] = []
    for relationship in relationships:
        wallet_a, wallet_b = sorted((relationship.source_wallet, relationship.target_wallet))
        markets = sorted({_normalize_market(market) for market in relationship.markets_seen if _normalize_market(market)})
        weight = relationship.evidence_count if relationship.evidence_count > 0 else len(markets)
        if weight <= 0 and not markets:
            continue
        edges.append(
            TraderEdge(
                wallet_a=wallet_a,
                wallet_b=wallet_b,
                shared_markets=markets,
                weight=weight,
            )
        )
    return edges


def _build_edge_markets(wallet_markets: dict[str, set[str]]) -> dict[tuple[str, str], set[str]]:
    market_wallets: dict[str, set[str]] = defaultdict(set)
    for wallet, markets in wallet_markets.items():
        for market in markets:
            market_wallets[market].add(wallet)

    edge_markets: dict[tuple[str, str], set[str]] = defaultdict(set)
    for market, wallets in market_wallets.items():
        ordered = sorted(wallets)
        for index, wallet_a in enumerate(ordered):
            for wallet_b in ordered[index + 1 :]:
                edge_markets[(wallet_a, wallet_b)].add(market)
    return edge_markets


def _edges_from_markets(edge_markets: dict[tuple[str, str], set[str]]) -> list[TraderEdge]:
    return [
        TraderEdge(
            wallet_a=wallet_a,
            wallet_b=wallet_b,
            shared_markets=sorted(markets),
            weight=len(markets),
        )
        for (wallet_a, wallet_b), markets in sorted(edge_markets.items())
        if markets
    ]


def _merge_edges(edges: list[TraderEdge]) -> list[TraderEdge]:
    merged: dict[tuple[str, str], TraderEdge] = {}
    for edge in edges:
        key = (edge.wallet_a, edge.wallet_b)
        current = merged.get(key)
        if current is None:
            merged[key] = TraderEdge(
                wallet_a=edge.wallet_a,
                wallet_b=edge.wallet_b,
                shared_markets=list(edge.shared_markets),
                weight=edge.weight,
            )
            continue
        current.shared_markets = sorted(set(current.shared_markets) | set(edge.shared_markets))
        current.weight = max(current.weight, edge.weight, len(current.shared_markets))
    return sorted(merged.values(), key=lambda item: (item.wallet_a, item.wallet_b))


def _build_adjacency(edges: list[TraderEdge]) -> dict[str, dict[str, int]]:
    adjacency: dict[str, dict[str, int]] = defaultdict(dict)
    for edge in edges:
        adjacency[edge.wallet_a][edge.wallet_b] = edge.weight
        adjacency[edge.wallet_b][edge.wallet_a] = edge.weight
    return adjacency


def _build_nodes(
    *,
    seed_wallets: set[str],
    wallet_markets: dict[str, set[str]],
    edges: list[TraderEdge],
    adjacency: dict[str, dict[str, int]],
    traders_db_path: str | Path,
    discovery_db_path: str | Path,
) -> dict[str, TraderNode]:
    wallets = set(seed_wallets)
    wallets.update(wallet_markets)
    wallets.update(edge.wallet_a for edge in edges)
    wallets.update(edge.wallet_b for edge in edges)

    trader_lookup = {
        trader.wallet: trader
        for trader in list_traders(limit=DEFAULT_NETWORK_LIMIT, db_path=str(traders_db_path))
    }
    nodes: dict[str, TraderNode] = {}
    for wallet in sorted(wallets):
        trader = trader_lookup.get(wallet)
        alpha = build_trader_alpha_report(wallet, traders_db_path=traders_db_path, discovery_db_path=discovery_db_path)
        neighbors = adjacency.get(wallet, {})
        degree = len(neighbors)
        weighted_degree = float(sum(neighbors.values()))
        nodes[wallet] = TraderNode(
            wallet=wallet,
            classification=trader.classification if trader else alpha.classification,
            watch_score=trader.watch_score if trader else alpha.watch_score,
            alpha_score=alpha.alpha_score,
            degree=degree,
            weighted_degree=weighted_degree,
        )
    return nodes


def _assign_clusters(nodes: dict[str, TraderNode], edges: list[TraderEdge]) -> dict[str, int]:
    parent = {wallet: wallet for wallet in nodes}

    def find(wallet: str) -> str:
        while parent[wallet] != wallet:
            parent[wallet] = parent[parent[wallet]]
            wallet = parent[wallet]
        return wallet

    def union(wallet_a: str, wallet_b: str) -> None:
        root_a = find(wallet_a)
        root_b = find(wallet_b)
        if root_a != root_b:
            parent[root_b] = root_a

    for edge in edges:
        if edge.wallet_a in parent and edge.wallet_b in parent:
            union(edge.wallet_a, edge.wallet_b)

    roots = sorted({find(wallet) for wallet in parent})
    root_to_cluster = {root: index for index, root in enumerate(roots)}
    cluster_ids = {wallet: root_to_cluster[find(wallet)] for wallet in parent}
    for wallet, cluster_id in cluster_ids.items():
        nodes[wallet].cluster_id = cluster_id
    return cluster_ids


def _apply_cluster_scores(
    nodes: dict[str, TraderNode],
    edges: list[TraderEdge],
    cluster_ids: dict[str, int],
) -> None:
    cluster_members: dict[int, list[str]] = defaultdict(list)
    for wallet, cluster_id in cluster_ids.items():
        cluster_members[cluster_id].append(wallet)

    cluster_density: dict[int, float] = {}
    for cluster_id, members in cluster_members.items():
        if len(members) <= 1:
            cluster_density[cluster_id] = 0.0
            continue
        member_set = set(members)
        internal_weight = sum(
            edge.weight
            for edge in edges
            if edge.wallet_a in member_set and edge.wallet_b in member_set
        )
        possible_edges = len(members) * (len(members) - 1) / 2
        cluster_density[cluster_id] = internal_weight / possible_edges if possible_edges else 0.0

    for node in nodes.values():
        members = cluster_members.get(node.cluster_id, [])
        node.cluster_score = len(members) * cluster_density.get(node.cluster_id, 0.0)


def _apply_bridge_scores(
    nodes: dict[str, TraderNode],
    adjacency: dict[str, dict[str, int]],
    cluster_ids: dict[str, int],
) -> None:
    for wallet, node in nodes.items():
        neighbor_clusters = {cluster_ids[neighbor] for neighbor in adjacency.get(wallet, {}) if neighbor in cluster_ids}
        own_cluster = cluster_ids.get(wallet, -1)
        cross_cluster_neighbors = sum(1 for cluster_id in neighbor_clusters if cluster_id != own_cluster)
        if node.degree <= 0:
            node.bridge_score = 0.0
            continue
        cluster_span = max(1, len(neighbor_clusters))
        node.bridge_score = (cross_cluster_neighbors / node.degree) * cluster_span


def _apply_centrality_scores(nodes: dict[str, TraderNode]) -> None:
    if not nodes:
        return
    max_weighted = max(node.weighted_degree for node in nodes.values()) or 1.0
    max_degree = max(node.degree for node in nodes.values()) or 1
    node_count = len(nodes)
    for node in nodes.values():
        degree_centrality = node.degree / max(1, node_count - 1)
        weighted_centrality = node.weighted_degree / max_weighted
        normalized_degree = node.degree / max_degree
        node.centrality_score = (degree_centrality * 0.35) + (weighted_centrality * 0.45) + (normalized_degree * 0.20)


def _apply_profitable_connection_scores(
    nodes: dict[str, TraderNode],
    adjacency: dict[str, dict[str, int]],
) -> None:
    for wallet, node in nodes.items():
        score = 0.0
        for neighbor, weight in adjacency.get(wallet, {}).items():
            neighbor_node = nodes.get(neighbor)
            if neighbor_node is None:
                continue
            if neighbor_node.alpha_score >= 50:
                score += neighbor_node.alpha_score * weight
        node.profitable_connection_score = score


def _ego_network(focus_wallet: str, adjacency: dict[str, dict[str, int]], depth: int) -> set[str]:
    visited = {focus_wallet}
    frontier = {focus_wallet}
    for _ in range(depth):
        next_frontier: set[str] = set()
        for wallet in frontier:
            for neighbor in adjacency.get(wallet, {}):
                if neighbor not in visited:
                    visited.add(neighbor)
                    next_frontier.add(neighbor)
        frontier = next_frontier
        if not frontier:
            break
    return visited


def _normalize_market(value: Any) -> str:
    market = str(value or "").strip().lower()
    return market


def _extract_events(payload: Any) -> list[Any]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("events", "activities", "activity", "payload", "data"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
    return []


def _market_key(event: dict[str, Any]) -> str:
    for key in ("condition_id", "conditionId", "market_id", "market", "market_slug", "slug", "market_title", "title"):
        value = str(event.get(key) or "").strip().lower()
        if value:
            return value
    raw = event.get("raw")
    if isinstance(raw, dict):
        return _market_key(raw)
    return ""
