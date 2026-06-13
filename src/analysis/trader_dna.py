from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from src.analysis.trader_alpha import build_trader_alpha_report
from src.analysis.trader_discovery import DEFAULT_TRADER_DISCOVERY_DB, load_discovered_wallets
from src.analysis.trader_network import build_trader_network
from src.analysis.trader_profiler import derive_specialization, profile_wallet
from src.analysis.trader_registry import DEFAULT_TRADERS_DB, list_traders, load_wallet_report
from src.analysis.trader_replay import build_trader_replay
from src.analysis.trader_scanner import DEFAULT_WALLET_EXPORT_DIR
from src.analysis.wallet_activity import validate_wallet

DEFAULT_DNA_LIMIT = 25
DEFAULT_CLUSTER_MIN_SIMILARITY = 75.0

BEHAVIOR_WEIGHT = 0.30
MARKET_WEIGHT = 0.25
INVENTORY_WEIGHT = 0.20
NETWORK_WEIGHT = 0.15
ALPHA_PROFILE_WEIGHT = 0.10

BEHAVIOR_FEATURES = (
    "asset_btc",
    "asset_eth",
    "asset_sol",
    "active_hour_norm",
    "active_day_norm",
    "session_length_norm",
    "events_per_day_norm",
)
MARKET_FEATURES = (
    "overlap_ratio",
    "markets_traded_norm",
    "shared_markets_norm",
)
INVENTORY_FEATURES = (
    "merge_ratio",
    "redeem_ratio",
    "buy_sell_ratio_norm",
)
NETWORK_FEATURES = (
    "degree_norm",
    "weighted_degree_norm",
)
ALPHA_PROFILE_FEATURES = (
    "alpha_norm",
    "watch_norm",
)

DAY_INDEX = {
    "Monday": 0,
    "Tuesday": 1,
    "Wednesday": 2,
    "Thursday": 3,
    "Friday": 4,
    "Saturday": 5,
    "Sunday": 6,
}


@dataclass
class TraderDNAVector:
    wallet: str
    features: dict[str, float]
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class TraderDNAMatch:
    wallet: str
    similarity_score: float
    shared_traits: list[str]
    category_scores: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def build_trader_dna_report(
    wallet: str,
    *,
    limit: int = DEFAULT_DNA_LIMIT,
    traders_db_path: str | Path = DEFAULT_TRADERS_DB,
    discovery_db_path: str | Path = DEFAULT_TRADER_DISCOVERY_DB,
    wallet_export_dir: str | Path = DEFAULT_WALLET_EXPORT_DIR,
) -> dict[str, Any]:
    wallet = _normalize_wallet(wallet)
    validate_wallet(wallet)
    focus = build_trader_dna_vector(
        wallet,
        traders_db_path=traders_db_path,
        discovery_db_path=discovery_db_path,
        wallet_export_dir=wallet_export_dir,
    )
    matches = most_similar_traders(
        wallet,
        limit=limit,
        traders_db_path=traders_db_path,
        discovery_db_path=discovery_db_path,
        wallet_export_dir=wallet_export_dir,
        focus_vector=focus,
    )
    return {
        "wallet": wallet,
        "dna_vector": focus.to_dict(),
        "top_matches": [match.to_dict() for match in matches],
    }


def most_similar_traders(
    wallet: str,
    *,
    limit: int = DEFAULT_DNA_LIMIT,
    traders_db_path: str | Path = DEFAULT_TRADERS_DB,
    discovery_db_path: str | Path = DEFAULT_TRADER_DISCOVERY_DB,
    wallet_export_dir: str | Path = DEFAULT_WALLET_EXPORT_DIR,
    focus_vector: TraderDNAVector | None = None,
) -> list[TraderDNAMatch]:
    wallet = _normalize_wallet(wallet)
    validate_wallet(wallet)
    focus = focus_vector or build_trader_dna_vector(
        wallet,
        traders_db_path=traders_db_path,
        discovery_db_path=discovery_db_path,
        wallet_export_dir=wallet_export_dir,
    )
    candidates = _candidate_wallets(
        wallet,
        limit=max(int(limit or DEFAULT_DNA_LIMIT), 1),
        traders_db_path=traders_db_path,
        discovery_db_path=discovery_db_path,
        wallet_export_dir=wallet_export_dir,
    )
    matches: list[TraderDNAMatch] = []
    for candidate_wallet in candidates:
        if candidate_wallet == wallet:
            continue
        candidate = build_trader_dna_vector(
            candidate_wallet,
            traders_db_path=traders_db_path,
            discovery_db_path=discovery_db_path,
            wallet_export_dir=wallet_export_dir,
        )
        score, category_scores = compute_similarity(focus, candidate)
        matches.append(
            TraderDNAMatch(
                wallet=candidate_wallet,
                similarity_score=score,
                shared_traits=_shared_traits(focus, candidate),
                category_scores=category_scores,
            )
        )
    matches.sort(key=lambda item: (-item.similarity_score, item.wallet))
    return matches[: max(int(limit or DEFAULT_DNA_LIMIT), 0)]


def dna_clusters(
    wallets: list[str] | None = None,
    *,
    min_similarity: float = DEFAULT_CLUSTER_MIN_SIMILARITY,
    limit: int = DEFAULT_DNA_LIMIT,
    traders_db_path: str | Path = DEFAULT_TRADERS_DB,
    discovery_db_path: str | Path = DEFAULT_TRADER_DISCOVERY_DB,
    wallet_export_dir: str | Path = DEFAULT_WALLET_EXPORT_DIR,
) -> dict[str, Any]:
    selected = _normalize_wallet_list(wallets) if wallets else [
        trader.wallet for trader in list_traders(limit=max(int(limit or DEFAULT_DNA_LIMIT), 1) * 4, db_path=str(traders_db_path))
    ]
    selected = selected[: max(int(limit or DEFAULT_DNA_LIMIT) * 4, 1)]
    vectors = {
        wallet: build_trader_dna_vector(
            wallet,
            traders_db_path=traders_db_path,
            discovery_db_path=discovery_db_path,
            wallet_export_dir=wallet_export_dir,
        )
        for wallet in selected
    }
    clusters = _cluster_vectors(vectors, min_similarity=min_similarity)
    return {
        "cluster_count": len(clusters),
        "min_similarity": min_similarity,
        "clusters": clusters,
    }


def build_trader_dna_vector(
    wallet: str,
    *,
    traders_db_path: str | Path = DEFAULT_TRADERS_DB,
    discovery_db_path: str | Path = DEFAULT_TRADER_DISCOVERY_DB,
    wallet_export_dir: str | Path = DEFAULT_WALLET_EXPORT_DIR,
) -> TraderDNAVector:
    wallet = _normalize_wallet(wallet)
    validate_wallet(wallet)

    alpha = build_trader_alpha_report(
        wallet,
        traders_db_path=traders_db_path,
        discovery_db_path=discovery_db_path,
    )
    registry = load_wallet_report(wallet, db_path=str(traders_db_path))
    metrics = {}
    if registry:
        metrics = registry.to_dict().get("report", {}).get("metrics") or {}

    profile = _safe_profile(
        wallet,
        traders_db_path=traders_db_path,
        discovery_db_path=discovery_db_path,
        wallet_export_dir=wallet_export_dir,
    )
    replay = _safe_replay(wallet, wallet_export_dir=wallet_export_dir)
    network_node = _network_node(
        wallet,
        traders_db_path=traders_db_path,
        discovery_db_path=discovery_db_path,
        wallet_export_dir=wallet_export_dir,
    )

    patterns = replay.patterns if replay else {}
    statistics = replay.statistics if replay else {}
    activity_count = max(int(alpha.activity_count or 0), int(statistics.get("activity_count") or 0), 1)
    trade_count = max(
        int(statistics.get("buy_count") or 0) + int(statistics.get("sell_count") or 0),
        int(metrics.get("trade_count") or 0),
        1,
    )
    buy_volume = float(metrics.get("buy_volume") or alpha.buy_volume or 0.0)
    sell_volume = float(metrics.get("sell_volume") or 0.0)
    trade_volume = buy_volume + sell_volume

    asset_total = max(
        float(alpha.asset_breakdown.get("BTC", 0.0))
        + float(alpha.asset_breakdown.get("ETH", 0.0))
        + float(alpha.asset_breakdown.get("SOL", 0.0)),
        1.0,
    )
    active_hour = patterns.get("most_active_hour_utc")
    active_day = patterns.get("most_active_day_of_week", "")

    features = {
        "asset_btc": _clamp(float(alpha.asset_breakdown.get("BTC", 0.0)) / asset_total),
        "asset_eth": _clamp(float(alpha.asset_breakdown.get("ETH", 0.0)) / asset_total),
        "asset_sol": _clamp(float(alpha.asset_breakdown.get("SOL", 0.0)) / asset_total),
        "active_hour_norm": _clamp(float(active_hour) / 23.0) if active_hour is not None else 0.0,
        "active_day_norm": _clamp(float(DAY_INDEX.get(str(active_day), 0)) / 6.0),
        "session_length_norm": _clamp(float(patterns.get("average_session_length_minutes") or 0.0) / 120.0),
        "events_per_day_norm": _clamp(float(patterns.get("average_events_per_day") or 0.0) / 100.0),
        "overlap_ratio": _clamp(float(alpha.overlap_ratio or 0.0)),
        "markets_traded_norm": _clamp(float(alpha.markets_traded or 0.0) / 500.0),
        "shared_markets_norm": _clamp(float(alpha.shared_markets or 0.0) / 200.0),
        "merge_ratio": _clamp(float(alpha.merge_count or 0.0) / activity_count),
        "redeem_ratio": _clamp(float(alpha.redeem_count or 0.0) / activity_count),
        "buy_sell_ratio_norm": _clamp(buy_volume / trade_volume) if trade_volume else 0.5,
        "degree_norm": _clamp(float(network_node.get("degree", 0)) / 50.0),
        "weighted_degree_norm": _clamp(float(network_node.get("weighted_degree", 0.0)) / 500.0),
        "alpha_norm": _clamp(float(alpha.alpha_score or 0.0) / 100.0),
        "watch_norm": _clamp(float(alpha.watch_score or 0.0) / 100.0),
    }

    metadata = {
        "specialization": profile.get("profile") or profile.get("specialization") or "unclassified trader",
        "classification": profile.get("classification") or alpha.classification,
        "preferred_assets": patterns.get("preferred_assets") or _preferred_assets_from_breakdown(alpha.asset_breakdown),
        "top_market_types": patterns.get("top_market_types") or [],
        "most_active_hour_utc": active_hour,
        "most_active_day_of_week": active_day,
        "merge_ratio": features["merge_ratio"],
        "redeem_ratio": features["redeem_ratio"],
        "overlap_ratio": features["overlap_ratio"],
        "shared_markets": int(alpha.shared_markets or 0),
        "degree": int(network_node.get("degree", 0)),
        "weighted_degree": float(network_node.get("weighted_degree", 0.0)),
        "alpha_score": int(alpha.alpha_score or 0),
        "watch_score": int(alpha.watch_score or 0),
        "markets_traded": int(alpha.markets_traded or 0),
    }
    return TraderDNAVector(wallet=wallet, features=features, metadata=metadata)


def compute_similarity(
    focus: TraderDNAVector,
    candidate: TraderDNAVector,
) -> tuple[float, dict[str, float]]:
    behavior = _numeric_similarity(focus.features, candidate.features, BEHAVIOR_FEATURES)
    market_numeric = _numeric_similarity(focus.features, candidate.features, MARKET_FEATURES)
    market_types = _jaccard(
        set(focus.metadata.get("top_market_types") or []),
        set(candidate.metadata.get("top_market_types") or []),
    )
    market = (0.65 * market_numeric) + (0.35 * market_types)

    inventory = _numeric_similarity(focus.features, candidate.features, INVENTORY_FEATURES)
    network = _numeric_similarity(focus.features, candidate.features, NETWORK_FEATURES)

    alpha_numeric = _numeric_similarity(focus.features, candidate.features, ALPHA_PROFILE_FEATURES)
    specialization_match = 1.0 if focus.metadata.get("specialization") == candidate.metadata.get("specialization") else 0.0
    classification_match = 1.0 if focus.metadata.get("classification") == candidate.metadata.get("classification") else 0.0
    alpha_profile = (0.45 * alpha_numeric) + (0.35 * specialization_match) + (0.20 * classification_match)

    category_scores = {
        "behavior": round(behavior * 100, 2),
        "market_participation": round(market * 100, 2),
        "inventory_style": round(inventory * 100, 2),
        "network_position": round(network * 100, 2),
        "alpha_profile": round(alpha_profile * 100, 2),
    }
    weighted = (
        (BEHAVIOR_WEIGHT * behavior)
        + (MARKET_WEIGHT * market)
        + (INVENTORY_WEIGHT * inventory)
        + (NETWORK_WEIGHT * network)
        + (ALPHA_PROFILE_WEIGHT * alpha_profile)
    )
    return round(min(100.0, weighted * 100.0), 2), category_scores


def _shared_traits(focus: TraderDNAVector, candidate: TraderDNAVector) -> list[str]:
    traits: list[str] = []
    focus_meta = focus.metadata
    candidate_meta = candidate.metadata

    specialization = str(candidate_meta.get("specialization") or "")
    if specialization and specialization == focus_meta.get("specialization"):
        traits.append(specialization)

    if focus_meta.get("classification") == candidate_meta.get("classification"):
        classification = str(candidate_meta.get("classification") or "").replace("_", " ")
        if classification:
            traits.append(f"same {classification} profile")

    shared_assets = set(focus_meta.get("preferred_assets") or []) & set(candidate_meta.get("preferred_assets") or [])
    for asset in sorted(shared_assets):
        traits.append(f"shared {asset} focus")

    shared_market_types = set(focus_meta.get("top_market_types") or []) & set(candidate_meta.get("top_market_types") or [])
    for market_type in sorted(shared_market_types)[:2]:
        traits.append(f"trades {market_type}")

    focus_hour = focus_meta.get("most_active_hour_utc")
    candidate_hour = candidate_meta.get("most_active_hour_utc")
    if focus_hour is not None and candidate_hour is not None and abs(int(focus_hour) - int(candidate_hour)) <= 2:
        traits.append("same active hours")

    if float(focus_meta.get("merge_ratio") or 0.0) >= 0.15 and float(candidate_meta.get("merge_ratio") or 0.0) >= 0.15:
        traits.append("high merge activity")

    if float(focus_meta.get("redeem_ratio") or 0.0) >= 0.15 and float(candidate_meta.get("redeem_ratio") or 0.0) >= 0.15:
        traits.append("high redeem activity")

    if int(candidate_meta.get("shared_markets") or 0) >= 5 and int(focus_meta.get("shared_markets") or 0) >= 5:
        traits.append("overlapping markets")

    deduped: list[str] = []
    seen: set[str] = set()
    for trait in traits:
        if trait in seen:
            continue
        seen.add(trait)
        deduped.append(trait)
    return deduped[:5]


def _candidate_wallets(
    focus_wallet: str,
    *,
    limit: int,
    traders_db_path: str | Path,
    discovery_db_path: str | Path,
    wallet_export_dir: str | Path,
) -> list[str]:
    pool: list[str] = []
    seen: set[str] = {focus_wallet}

    for trader in list_traders(limit=max(limit * 6, 50), db_path=str(traders_db_path)):
        if trader.wallet not in seen:
            seen.add(trader.wallet)
            pool.append(trader.wallet)

    for candidate in load_discovered_wallets(discovery_db_path, limit=max(limit * 4, 40)):
        if candidate.wallet not in seen:
            seen.add(candidate.wallet)
            pool.append(candidate.wallet)

    network = build_trader_network(
        wallet=focus_wallet,
        traders_db_path=traders_db_path,
        discovery_db_path=discovery_db_path,
        wallet_export_dir=wallet_export_dir,
    )
    for edge in network.edges:
        for wallet in (edge.wallet_a, edge.wallet_b):
            if wallet not in seen:
                seen.add(wallet)
                pool.append(wallet)

    for node in sorted(
        network.nodes.values(),
        key=lambda item: (-item.weighted_degree, -item.degree, item.wallet),
    ):
        if node.wallet not in seen:
            seen.add(node.wallet)
            pool.append(node.wallet)

    return pool


def _cluster_vectors(
    vectors: dict[str, TraderDNAVector],
    *,
    min_similarity: float,
) -> list[dict[str, Any]]:
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

    threshold = min_similarity / 100.0
    for index, left_wallet in enumerate(wallets):
        left = vectors[left_wallet]
        for right_wallet in wallets[index + 1 :]:
            score, _ = compute_similarity(left, vectors[right_wallet])
            if score / 100.0 >= threshold:
                union(left_wallet, right_wallet)

    grouped: dict[str, list[str]] = {}
    for wallet in wallets:
        root = find(wallet)
        grouped.setdefault(root, []).append(wallet)

    clusters: list[dict[str, Any]] = []
    for cluster_id, members in enumerate(sorted(grouped.values(), key=lambda group: (-len(group), group[0]))):
        specializations = [vectors[wallet].metadata.get("specialization") or "unclassified trader" for wallet in members]
        family_label = max(set(specializations), key=specializations.count)
        centroid = members[0]
        clusters.append(
            {
                "cluster_id": cluster_id,
                "family_label": family_label,
                "member_count": len(members),
                "centroid_wallet": centroid,
                "members": members,
            }
        )
    return clusters


def _safe_profile(
    wallet: str,
    *,
    traders_db_path: str | Path,
    discovery_db_path: str | Path,
    wallet_export_dir: str | Path,
) -> dict[str, Any]:
    try:
        return profile_wallet(
            wallet,
            scan_if_missing=False,
            traders_db_path=traders_db_path,
            discovery_db_path=discovery_db_path,
            wallet_export_dir=wallet_export_dir,
        ).to_dict()
    except Exception:
        alpha = build_trader_alpha_report(
            wallet,
            traders_db_path=traders_db_path,
            discovery_db_path=discovery_db_path,
        )
        specialization = derive_specialization(
            classification=alpha.classification,
            confidence=alpha.confidence,
            activity_count=alpha.activity_count,
            markets_traded=alpha.markets_traded,
            shared_markets=alpha.shared_markets,
            overlap_ratio=alpha.overlap_ratio,
            merge_count=alpha.merge_count,
            redeem_count=alpha.redeem_count,
            btc_volume=alpha.asset_breakdown.get("BTC", 0.0),
            eth_volume=alpha.asset_breakdown.get("ETH", 0.0),
            sol_volume=alpha.asset_breakdown.get("SOL", 0.0),
            other_volume=alpha.asset_breakdown.get("OTHER", 0.0),
        )
        return {
            "wallet": wallet,
            "profile": specialization,
            "specialization": specialization,
            "classification": alpha.classification,
        }


def _safe_replay(wallet: str, *, wallet_export_dir: str | Path):
    try:
        return build_trader_replay(
            wallet,
            wallet_export_dir=wallet_export_dir,
            export_if_missing=False,
            include_timeline=False,
        )
    except Exception:
        return None


def _network_node(
    wallet: str,
    *,
    traders_db_path: str | Path,
    discovery_db_path: str | Path,
    wallet_export_dir: str | Path,
) -> dict[str, Any]:
    network = build_trader_network(
        wallet=wallet,
        traders_db_path=traders_db_path,
        discovery_db_path=discovery_db_path,
        wallet_export_dir=wallet_export_dir,
    )
    node = network.nodes.get(wallet)
    if node is None:
        return {"degree": 0, "weighted_degree": 0.0}
    return {"degree": node.degree, "weighted_degree": node.weighted_degree}


def _preferred_assets_from_breakdown(asset_breakdown: dict[str, float]) -> list[str]:
    ordered = sorted(
        (
            (asset, float(asset_breakdown.get(asset, 0.0) or 0.0))
            for asset in ("BTC", "ETH", "SOL")
            if float(asset_breakdown.get(asset, 0.0) or 0.0) > 0
        ),
        key=lambda item: (-item[1], item[0]),
    )
    return [asset for asset, _value in ordered]


def _numeric_similarity(left: dict[str, float], right: dict[str, float], keys: tuple[str, ...]) -> float:
    if not keys:
        return 0.0
    total = 0.0
    for key in keys:
        total += 1.0 - min(1.0, abs(float(left.get(key, 0.0)) - float(right.get(key, 0.0))))
    return total / len(keys)


def _jaccard(left: set[str], right: set[str]) -> float:
    if not left and not right:
        return 1.0
    union = left | right
    if not union:
        return 0.0
    return len(left & right) / len(union)


def _normalize_wallet(wallet: str) -> str:
    return str(wallet or "").strip().lower()


def _normalize_wallet_list(wallets: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for wallet in wallets:
        normalized = _normalize_wallet(wallet)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        ordered.append(normalized)
    return ordered


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, float(value or 0.0)))
