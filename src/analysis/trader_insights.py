from __future__ import annotations

from pathlib import Path
from typing import Any

from src.analysis.trader_alpha import build_trader_alpha_report
from src.analysis.trader_discovery import DEFAULT_TRADER_DISCOVERY_DB
from src.analysis.trader_network import TraderNetwork, build_trader_network, network_summary
from src.analysis.trader_profiler import DEFAULT_PROFILE_LIMIT, derive_specialization, profile_wallet
from src.analysis.trader_registry import DEFAULT_TRADERS_DB
from src.analysis.trader_scanner import DEFAULT_WALLET_EXPORT_DIR

DEFAULT_INSIGHT_LIMIT = 25
MARKET_MAKER_PROFILES = {"crypto market maker", "market maker", "high-frequency inventory manager"}


def build_trader_insight_report(
    seed_wallet: str,
    *,
    limit: int = DEFAULT_INSIGHT_LIMIT,
    scan_if_missing: bool = False,
    traders_db_path: str | Path = DEFAULT_TRADERS_DB,
    discovery_db_path: str | Path = DEFAULT_TRADER_DISCOVERY_DB,
    wallet_export_dir: str | Path = DEFAULT_WALLET_EXPORT_DIR,
) -> dict[str, Any]:
    seed_wallet = str(seed_wallet or "").strip().lower()
    network = build_trader_network(
        wallet=seed_wallet,
        traders_db_path=traders_db_path,
        discovery_db_path=discovery_db_path,
        wallet_export_dir=wallet_export_dir,
    )
    summary = network_summary(network, limit=limit, focus_wallet=seed_wallet)
    profiled = _profile_candidates(
        network=network,
        seed_wallet=seed_wallet,
        summary=summary,
        limit=limit,
        scan_if_missing=scan_if_missing,
        traders_db_path=traders_db_path,
        discovery_db_path=discovery_db_path,
        wallet_export_dir=wallet_export_dir,
    )

    return {
        "seed_wallet": seed_wallet,
        "summary": {
            "wallets_analyzed": len(network.nodes),
            "edges": len(network.edges),
            "clusters": network.clusters,
        },
        "top_alpha_traders": _top_alpha_traders(profiled, network, limit=limit),
        "top_connected_traders": _enrich_network_rows(summary["top_connected_wallets"], profiled),
        "top_btc_specialists": _filter_profiles(profiled, profile="btc specialist", limit=limit),
        "top_directional_traders": _filter_profiles(profiled, profile="directional trader", limit=limit),
        "top_market_makers": _top_market_makers(profiled, limit=limit),
        "strongest_shared_market_relationships": _strongest_relationships(network, seed_wallet, limit=limit),
        "recommended_traders": recommend_traders(
            profiled,
            network=network,
            seed_wallet=seed_wallet,
            limit=limit,
        ),
    }


def recommend_traders(
    profiles: dict[str, dict[str, Any]],
    *,
    network: TraderNetwork,
    seed_wallet: str,
    limit: int = DEFAULT_INSIGHT_LIMIT,
) -> list[dict[str, Any]]:
    seed_wallet = str(seed_wallet or "").strip().lower()
    recommendations: list[dict[str, Any]] = []
    for wallet, profile in profiles.items():
        if wallet == seed_wallet:
            continue
        node = network.nodes.get(wallet)
        entry = {
            "wallet": wallet,
            "profile": profile["profile"],
            "alpha_score": profile["alpha_score"],
            "watch_score": profile["watch_score"],
            "shared_markets": profile["shared_markets"],
            "weighted_degree": round(node.weighted_degree, 2) if node else 0.0,
            "degree": node.degree if node else 0,
            "recommendation_score": _recommendation_score(profile, node),
        }
        entry["reason"] = _recommendation_reason(entry)
        recommendations.append(entry)

    recommendations.sort(
        key=lambda item: (
            -item["recommendation_score"],
            -item["alpha_score"],
            -item["shared_markets"],
            item["wallet"],
        )
    )
    return [
        {
            "wallet": item["wallet"],
            "profile": item["profile"],
            "alpha_score": item["alpha_score"],
            "watch_score": item["watch_score"],
            "shared_markets": item["shared_markets"],
            "reason": item["reason"],
        }
        for item in recommendations[: max(int(limit or DEFAULT_INSIGHT_LIMIT), 0)]
    ]


def _profile_candidates(
    *,
    network: TraderNetwork,
    seed_wallet: str,
    summary: dict[str, Any],
    limit: int,
    scan_if_missing: bool,
    traders_db_path: str | Path,
    discovery_db_path: str | Path,
    wallet_export_dir: str | Path,
) -> dict[str, dict[str, Any]]:
    wallets = _candidate_wallets(network, seed_wallet, summary, limit=limit)
    profiles: dict[str, dict[str, Any]] = {}
    for wallet in wallets:
        profiles[wallet] = _build_profile_entry(
            wallet,
            scan_if_missing=scan_if_missing,
            traders_db_path=traders_db_path,
            discovery_db_path=discovery_db_path,
            wallet_export_dir=wallet_export_dir,
        )
    return profiles


def _candidate_wallets(
    network: TraderNetwork,
    seed_wallet: str,
    summary: dict[str, Any],
    *,
    limit: int,
) -> list[str]:
    wallets: list[str] = [seed_wallet]
    wallets.extend(row["wallet"] for row in summary.get("top_connected_wallets", []))
    wallets.extend(row["wallet"] for row in summary.get("focus_neighbors", []))
    ranked_nodes = sorted(
        network.nodes.values(),
        key=lambda node: (-node.alpha_score, -node.weighted_degree, -node.degree, node.wallet),
    )
    wallets.extend(node.wallet for node in ranked_nodes[: max(limit * 4, limit)])
    return _dedupe_wallets(wallets)


def _build_profile_entry(
    wallet: str,
    *,
    scan_if_missing: bool,
    traders_db_path: str | Path,
    discovery_db_path: str | Path,
    wallet_export_dir: str | Path,
) -> dict[str, Any]:
    if scan_if_missing:
        profile = profile_wallet(
            wallet,
            scan_if_missing=True,
            traders_db_path=traders_db_path,
            discovery_db_path=discovery_db_path,
            wallet_export_dir=wallet_export_dir,
        )
        return profile.to_dict()

    alpha = build_trader_alpha_report(
        wallet,
        traders_db_path=traders_db_path,
        discovery_db_path=discovery_db_path,
    )
    profile_label = derive_specialization(
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
        "profile": profile_label,
        "classification": alpha.classification,
        "confidence": alpha.confidence,
        "alpha_score": alpha.alpha_score,
        "watch_score": alpha.watch_score,
        "markets_traded": alpha.markets_traded,
        "shared_markets": alpha.shared_markets,
        "btc_volume": alpha.asset_breakdown.get("BTC", 0.0),
        "eth_volume": alpha.asset_breakdown.get("ETH", 0.0),
        "sol_volume": alpha.asset_breakdown.get("SOL", 0.0),
        "merge_count": alpha.merge_count,
        "redeem_count": alpha.redeem_count,
        "specialization": profile_label,
    }


def _top_alpha_traders(
    profiles: dict[str, dict[str, Any]],
    network: TraderNetwork,
    *,
    limit: int,
) -> list[dict[str, Any]]:
    ordered = sorted(
        profiles.values(),
        key=lambda item: (
            -item["alpha_score"],
            -_node_weighted_degree(network, item["wallet"]),
            item["wallet"],
        ),
    )
    return [_insight_row(item, network) for item in ordered[: max(int(limit or DEFAULT_INSIGHT_LIMIT), 0)]]


def _top_market_makers(profiles: dict[str, dict[str, Any]], *, limit: int) -> list[dict[str, Any]]:
    makers = [
        item
        for item in profiles.values()
        if item["profile"] in MARKET_MAKER_PROFILES or item["classification"] == "market_maker"
    ]
    makers.sort(key=lambda item: (-item["alpha_score"], -item["watch_score"], -item["shared_markets"], item["wallet"]))
    return [_compact_profile_row(item) for item in makers[: max(int(limit or DEFAULT_INSIGHT_LIMIT), 0)]]


def _filter_profiles(
    profiles: dict[str, dict[str, Any]],
    *,
    profile: str,
    limit: int,
) -> list[dict[str, Any]]:
    rows = [item for item in profiles.values() if item["profile"] == profile]
    rows.sort(key=lambda item: (-item["alpha_score"], -item["shared_markets"], -item["watch_score"], item["wallet"]))
    return [_compact_profile_row(item) for item in rows[: max(int(limit or DEFAULT_INSIGHT_LIMIT), 0)]]


def _strongest_relationships(
    network: TraderNetwork,
    seed_wallet: str,
    *,
    limit: int,
) -> list[dict[str, Any]]:
    edges = list(network.edges)
    edges.sort(key=lambda edge: (-edge.weight, edge.wallet_a, edge.wallet_b))
    rows: list[dict[str, Any]] = []
    for edge in edges:
        if seed_wallet and seed_wallet not in {edge.wallet_a, edge.wallet_b}:
            continue
        rows.append(
            {
                "wallet_a": edge.wallet_a,
                "wallet_b": edge.wallet_b,
                "shared_markets": edge.weight,
                "weight": edge.weight,
                "market_count": len(edge.shared_markets),
            }
        )
        if len(rows) >= max(int(limit or DEFAULT_INSIGHT_LIMIT), 0):
            break
    if rows:
        return rows

    for edge in edges[: max(int(limit or DEFAULT_INSIGHT_LIMIT), 0)]:
        rows.append(
            {
                "wallet_a": edge.wallet_a,
                "wallet_b": edge.wallet_b,
                "shared_markets": edge.weight,
                "weight": edge.weight,
                "market_count": len(edge.shared_markets),
            }
        )
    return rows


def _recommendation_score(profile: dict[str, Any], node: Any | None) -> float:
    score = float(profile.get("alpha_score") or 0) * 0.35
    score += min(float(profile.get("shared_markets") or 0), 200.0) * 0.25
    score += min(float(profile.get("watch_score") or 0), 100.0) * 0.15
    if node is not None:
        score += min(float(node.weighted_degree), 500.0) * 0.15
        score += min(float(node.centrality_score) * 100.0, 25.0)
    if profile.get("profile") in {"btc specialist", "cross-market arbitrage", "crypto market maker"}:
        score += 8.0
    return round(score, 2)


def _recommendation_reason(entry: dict[str, Any]) -> str:
    reasons: list[str] = []
    profile = str(entry.get("profile") or "")
    alpha_score = int(entry.get("alpha_score") or 0)
    shared_markets = int(entry.get("shared_markets") or 0)
    weighted_degree = float(entry.get("weighted_degree") or 0.0)

    if profile == "btc specialist":
        reasons.append("high BTC specialization")
    elif profile == "directional trader":
        reasons.append("directional trading behavior")
    elif profile == "cross-market arbitrage":
        reasons.append("cross-market arbitrage patterns")
    elif profile in MARKET_MAKER_PROFILES:
        reasons.append("market-making activity")

    if shared_markets >= 50:
        reasons.append("strong co-market evidence")
    elif shared_markets >= 10:
        reasons.append("repeat co-market overlap with seed network")

    if alpha_score >= 70:
        reasons.append("high alpha study value")
    elif alpha_score >= 40:
        reasons.append("moderate alpha study value")

    if weighted_degree >= 100:
        reasons.append("central network position")

    if not reasons:
        reasons.append("discovered near seed wallet with study potential")

    sentence = ", ".join(reasons)
    return sentence[0].upper() + sentence[1:] if sentence else "Discovered near seed wallet with study potential"


def _enrich_network_rows(
    rows: list[dict[str, Any]],
    profiles: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    enriched: list[dict[str, Any]] = []
    for row in rows:
        wallet = row["wallet"]
        profile = profiles.get(wallet, {})
        enriched.append(
            {
                "wallet": wallet,
                "profile": profile.get("profile", "unclassified trader"),
                "alpha_score": profile.get("alpha_score", row.get("alpha_score", 0)),
                "watch_score": profile.get("watch_score", row.get("watch_score", 0)),
                "degree": row.get("degree", 0),
                "weighted_degree": row.get("weighted_degree", 0.0),
                "shared_markets": profile.get("shared_markets", 0),
                "centrality_score": row.get("centrality_score", 0.0),
            }
        )
    return enriched


def _insight_row(profile: dict[str, Any], network: TraderNetwork) -> dict[str, Any]:
    node = network.nodes.get(profile["wallet"])
    row = _compact_profile_row(profile)
    if node:
        row["degree"] = node.degree
        row["weighted_degree"] = round(node.weighted_degree, 2)
    return row


def _compact_profile_row(profile: dict[str, Any]) -> dict[str, Any]:
    return {
        "wallet": profile["wallet"],
        "profile": profile["profile"],
        "alpha_score": profile["alpha_score"],
        "watch_score": profile["watch_score"],
        "shared_markets": profile["shared_markets"],
        "markets_traded": profile["markets_traded"],
        "classification": profile["classification"],
    }


def _node_weighted_degree(network: TraderNetwork, wallet: str) -> float:
    node = network.nodes.get(wallet)
    return node.weighted_degree if node else 0.0


def _dedupe_wallets(wallets: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for wallet in wallets:
        normalized = str(wallet or "").strip().lower()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        ordered.append(normalized)
    return ordered
