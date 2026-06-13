from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from src.analysis.trader_alpha import build_trader_alpha_report
from src.analysis.trader_discovery import DEFAULT_TRADER_DISCOVERY_DB, load_discovered_wallets
from src.analysis.trader_dna import TraderDNAVector, build_trader_dna_vector, compute_similarity
from src.analysis.trader_network import TraderNetwork, build_trader_network
from src.analysis.trader_registry import DEFAULT_TRADERS_DB, list_traders
from src.analysis.trader_scanner import DEFAULT_WALLET_EXPORT_DIR

DEFAULT_FAMILY_LIMIT = 25
DEFAULT_MIN_AFFINITY = 0.65
DEFAULT_MIN_DNA_SIMILARITY = 70.0
DEFAULT_MIN_EDGE_WEIGHT = 5
TOP_WALLET_LIMIT = 10

FAMILY_TYPES = (
    "BTC Specialists",
    "ETH Specialists",
    "SOL Specialists",
    "Market Makers",
    "Inventory Managers",
    "Cross-Market Arbitrage",
    "Directional Traders",
    "Hybrid Groups",
)


@dataclass
class TraderFamily:
    family_id: str
    size: int
    dominant_profile: str
    dominant_specialization: str
    family_type: str
    average_alpha: float
    average_watch_score: float
    top_wallets: list[str]
    shared_traits: list[str]
    members: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload.pop("members", None)
        return payload


def discover_trader_families(
    wallets: list[str] | None = None,
    *,
    limit: int = DEFAULT_FAMILY_LIMIT,
    min_affinity: float = DEFAULT_MIN_AFFINITY,
    min_dna_similarity: float = DEFAULT_MIN_DNA_SIMILARITY,
    min_edge_weight: int = DEFAULT_MIN_EDGE_WEIGHT,
    traders_db_path: str | Path = DEFAULT_TRADERS_DB,
    discovery_db_path: str | Path = DEFAULT_TRADER_DISCOVERY_DB,
    wallet_export_dir: str | Path = DEFAULT_WALLET_EXPORT_DIR,
) -> list[TraderFamily]:
    selected = _select_wallets(
        wallets,
        limit=max(int(limit or DEFAULT_FAMILY_LIMIT), 1) * 6,
        traders_db_path=traders_db_path,
        discovery_db_path=discovery_db_path,
    )
    if not selected:
        return []

    vectors = {
        wallet: build_trader_dna_vector(
            wallet,
            traders_db_path=traders_db_path,
            discovery_db_path=discovery_db_path,
            wallet_export_dir=wallet_export_dir,
        )
        for wallet in selected
    }
    network = build_trader_network(
        traders_db_path=traders_db_path,
        discovery_db_path=discovery_db_path,
        wallet_export_dir=wallet_export_dir,
    )
    edge_lookup = _edge_lookup(network)
    groups = _cluster_family_groups(
        vectors,
        network=network,
        edge_lookup=edge_lookup,
        min_affinity=min_affinity,
        min_dna_similarity=min_dna_similarity,
        min_edge_weight=min_edge_weight,
    )
    families = [
        _build_family(family_id=f"family_{index:02d}", members=members, vectors=vectors)
        for index, members in enumerate(groups, start=1)
    ]
    families.sort(key=lambda item: (-item.size, -item.average_alpha, item.family_id))
    return families[: max(int(limit or DEFAULT_FAMILY_LIMIT), 0)]


def build_trader_family_report(
    *,
    family_id: str | None = None,
    limit: int = DEFAULT_FAMILY_LIMIT,
    traders_db_path: str | Path = DEFAULT_TRADERS_DB,
    discovery_db_path: str | Path = DEFAULT_TRADER_DISCOVERY_DB,
    wallet_export_dir: str | Path = DEFAULT_WALLET_EXPORT_DIR,
) -> dict[str, Any]:
    families = discover_trader_families(
        limit=limit,
        traders_db_path=traders_db_path,
        discovery_db_path=discovery_db_path,
        wallet_export_dir=wallet_export_dir,
    )
    if family_id:
        match = next((family for family in families if family.family_id == family_id), None)
        if match is None:
            return {"family_id": family_id, "error": "family not found"}
        return match.to_dict()

    return {
        "family_count": len(families),
        "families": [family.to_dict() for family in families],
        "largest_families": [family.to_dict() for family in _largest_families(families)],
        "highest_alpha_families": [family.to_dict() for family in _highest_alpha_families(families)],
        "highest_watch_score_families": [family.to_dict() for family in _highest_watch_families(families)],
    }


def get_trader_family(
    family_id: str,
    *,
    limit: int = DEFAULT_FAMILY_LIMIT,
    traders_db_path: str | Path = DEFAULT_TRADERS_DB,
    discovery_db_path: str | Path = DEFAULT_TRADER_DISCOVERY_DB,
    wallet_export_dir: str | Path = DEFAULT_WALLET_EXPORT_DIR,
) -> dict[str, Any] | None:
    report = build_trader_family_report(
        family_id=family_id,
        limit=limit,
        traders_db_path=traders_db_path,
        discovery_db_path=discovery_db_path,
        wallet_export_dir=wallet_export_dir,
    )
    if report.get("error"):
        return None
    return report


def _largest_families(families: list[TraderFamily], *, limit: int = 5) -> list[TraderFamily]:
    return sorted(families, key=lambda item: (-item.size, -item.average_alpha, item.family_id))[:limit]


def _highest_alpha_families(families: list[TraderFamily], *, limit: int = 5) -> list[TraderFamily]:
    return sorted(families, key=lambda item: (-item.average_alpha, -item.size, item.family_id))[:limit]


def _highest_watch_families(families: list[TraderFamily], *, limit: int = 5) -> list[TraderFamily]:
    return sorted(families, key=lambda item: (-item.average_watch_score, -item.size, item.family_id))[:limit]


def _build_family(
    *,
    family_id: str,
    members: list[str],
    vectors: dict[str, TraderDNAVector],
) -> TraderFamily:
    member_vectors = [vectors[wallet] for wallet in members if wallet in vectors]
    classifications = [str(item.metadata.get("classification") or "unknown") for item in member_vectors]
    specializations = [str(item.metadata.get("specialization") or "unclassified trader") for item in member_vectors]
    dominant_profile = Counter(classifications).most_common(1)[0][0] if classifications else "unknown"
    dominant_specialization = Counter(specializations).most_common(1)[0][0] if specializations else "unclassified trader"

    alpha_scores = [int(item.metadata.get("alpha_score") or 0) for item in member_vectors]
    watch_scores = [int(item.metadata.get("watch_score") or 0) for item in member_vectors]
    average_alpha = round(sum(alpha_scores) / len(alpha_scores), 2) if alpha_scores else 0.0
    average_watch_score = round(sum(watch_scores) / len(watch_scores), 2) if watch_scores else 0.0

    top_wallets = [
        wallet
        for wallet, _score in sorted(
            ((wallet, int(vectors[wallet].metadata.get("alpha_score") or 0)) for wallet in members if wallet in vectors),
            key=lambda item: (-item[1], item[0]),
        )
    ][:TOP_WALLET_LIMIT]

    family = TraderFamily(
        family_id=family_id,
        size=len(members),
        dominant_profile=dominant_profile,
        dominant_specialization=dominant_specialization,
        family_type=_detect_family_type(dominant_profile, dominant_specialization, member_vectors),
        average_alpha=average_alpha,
        average_watch_score=average_watch_score,
        top_wallets=top_wallets,
        shared_traits=_family_shared_traits(member_vectors),
        members=sorted(members),
    )
    return family


def _detect_family_type(
    dominant_profile: str,
    dominant_specialization: str,
    member_vectors: list[TraderDNAVector],
) -> str:
    specialization = dominant_specialization.lower()
    profile = dominant_profile.lower()

    asset_counter: Counter[str] = Counter()
    for vector in member_vectors:
        for asset in vector.metadata.get("preferred_assets") or []:
            asset_counter[str(asset)] += 1
    top_asset = asset_counter.most_common(1)[0][0] if asset_counter else ""

    if "btc specialist" in specialization or (top_asset == "BTC" and "specialist" in specialization):
        return "BTC Specialists"
    if "eth" in specialization and "specialist" in specialization:
        return "ETH Specialists"
    if top_asset == "SOL" or ("sol" in specialization and "specialist" in specialization):
        return "SOL Specialists"
    if profile == "market_maker" or "market maker" in specialization:
        return "Market Makers"
    if "inventory" in specialization:
        return "Inventory Managers"
    if "arbitrage" in specialization or profile == "arbitrage_trader":
        return "Cross-Market Arbitrage"
    if profile == "quantitative_directional" or "directional" in specialization:
        return "Directional Traders"
    return "Hybrid Groups"


def _family_shared_traits(member_vectors: list[TraderDNAVector]) -> list[str]:
    if not member_vectors:
        return []

    traits: list[str] = []
    asset_counter: Counter[str] = Counter()
    market_type_counter: Counter[str] = Counter()
    merge_ratios: list[float] = []
    shared_markets: list[int] = []

    for vector in member_vectors:
        for asset in vector.metadata.get("preferred_assets") or []:
            asset_counter[str(asset)] += 1
        for market_type in vector.metadata.get("top_market_types") or []:
            market_type_counter[str(market_type)] += 1
        merge_ratios.append(float(vector.metadata.get("merge_ratio") or 0.0))
        shared_markets.append(int(vector.metadata.get("shared_markets") or 0))

    if asset_counter:
        top_asset, count = asset_counter.most_common(1)[0]
        if count >= max(1, len(member_vectors) // 2):
            traits.append(f"{top_asset} focus")

    if market_type_counter:
        top_market, count = market_type_counter.most_common(1)[0]
        if count >= max(1, len(member_vectors) // 2):
            traits.append(_humanize_market_type(top_market))

    if merge_ratios and (sum(merge_ratios) / len(merge_ratios)) >= 0.15:
        traits.append("high merge activity")

    if shared_markets and (sum(shared_markets) / len(shared_markets)) >= 5:
        traits.append("overlapping markets")

    specialization_counter = Counter(str(vector.metadata.get("specialization") or "") for vector in member_vectors)
    top_specialization, count = specialization_counter.most_common(1)[0]
    if top_specialization and count >= max(1, len(member_vectors) // 2):
        traits.append(top_specialization)

    deduped: list[str] = []
    seen: set[str] = set()
    for trait in traits:
        if trait in seen:
            continue
        seen.add(trait)
        deduped.append(trait)
    return deduped[:5]


def _cluster_family_groups(
    vectors: dict[str, TraderDNAVector],
    *,
    network: TraderNetwork,
    edge_lookup: dict[tuple[str, str], int],
    min_affinity: float,
    min_dna_similarity: float,
    min_edge_weight: int,
) -> list[list[str]]:
    wallets = sorted(vectors)
    if not wallets:
        return []

    parent = {wallet: wallet for wallet in wallets}

    def find(wallet: str) -> str:
        while parent[wallet] != wallet:
            parent[wallet] = parent[parent[wallet]]
            wallet = parent[wallet]
        return wallet

    def union(left: str, right: str) -> None:
        parent[find(left)] = find(right)

    for index, left_wallet in enumerate(wallets):
        left_vector = vectors[left_wallet]
        for right_wallet in wallets[index + 1 :]:
            right_vector = vectors[right_wallet]
            dna_score, _ = compute_similarity(left_vector, right_vector)
            edge_weight = _edge_weight(edge_lookup, left_wallet, right_wallet)
            affinity = _family_affinity(
                left_vector,
                right_vector,
                dna_score=dna_score,
                edge_weight=edge_weight,
                network=network,
            )
            if (
                affinity >= min_affinity
                or dna_score >= min_dna_similarity
                or edge_weight >= min_edge_weight
            ):
                union(left_wallet, right_wallet)

    grouped: dict[str, list[str]] = {}
    for wallet in wallets:
        root = find(wallet)
        grouped.setdefault(root, []).append(wallet)

    return sorted(grouped.values(), key=lambda group: (-len(group), group[0]))


def _family_affinity(
    left: TraderDNAVector,
    right: TraderDNAVector,
    *,
    dna_score: float,
    edge_weight: int,
    network: TraderNetwork,
) -> float:
    dna_component = dna_score / 100.0

    left_shared = int(left.metadata.get("shared_markets") or 0)
    right_shared = int(right.metadata.get("shared_markets") or 0)
    market_component = min(left_shared, right_shared) / max(left_shared, right_shared, 1)

    market_types = _jaccard(
        set(left.metadata.get("top_market_types") or []),
        set(right.metadata.get("top_market_types") or []),
    )

    if edge_weight > 0:
        network_component = min(1.0, edge_weight / 50.0)
    else:
        left_node = network.nodes.get(left.wallet)
        right_node = network.nodes.get(right.wallet)
        same_cluster = (
            left_node is not None
            and right_node is not None
            and left_node.cluster_id >= 0
            and left_node.cluster_id == right_node.cluster_id
        )
        network_component = 1.0 if same_cluster else 0.0

    return (0.45 * dna_component) + (0.25 * market_component) + (0.20 * network_component) + (0.10 * market_types)


def _select_wallets(
    wallets: list[str] | None,
    *,
    limit: int,
    traders_db_path: str | Path,
    discovery_db_path: str | Path,
) -> list[str]:
    if wallets:
        return _normalize_wallet_list(wallets)[:limit]

    seen: set[str] = set()
    ordered: list[str] = []
    for trader in list_traders(limit=limit, db_path=str(traders_db_path)):
        if trader.wallet not in seen:
            seen.add(trader.wallet)
            ordered.append(trader.wallet)
    for candidate in load_discovered_wallets(discovery_db_path, limit=limit):
        if candidate.wallet not in seen:
            seen.add(candidate.wallet)
            ordered.append(candidate.wallet)
    return ordered[:limit]


def _edge_lookup(network: TraderNetwork) -> dict[tuple[str, str], int]:
    lookup: dict[tuple[str, str], int] = {}
    for edge in network.edges:
        pair = tuple(sorted((edge.wallet_a, edge.wallet_b)))
        lookup[pair] = max(int(edge.weight or 0), lookup.get(pair, 0))
    return lookup


def _edge_weight(edge_lookup: dict[tuple[str, str], int], left: str, right: str) -> int:
    return edge_lookup.get(tuple(sorted((left, right))), 0)


def _humanize_market_type(market_type: str) -> str:
    text = str(market_type or "")
    if "5m" in text:
        return "5 minute markets"
    if "15m" in text:
        return "15 minute markets"
    if "hourly" in text:
        return "hourly markets"
    return text


def _jaccard(left: set[str], right: set[str]) -> float:
    if not left and not right:
        return 1.0
    union = left | right
    if not union:
        return 0.0
    return len(left & right) / len(union)


def _normalize_wallet_list(wallets: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for wallet in wallets:
        normalized = str(wallet or "").strip().lower()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        ordered.append(normalized)
    return ordered
