"""Read-only leaderboard wallet attribution and performance analytics.

All functions in this module are analytics-only. They do not write to any
production database, do not place orders, do not call authenticated APIs,
and do not mutate wallet state.
"""
from __future__ import annotations

import json
import math
import statistics
from collections import defaultdict
from pathlib import Path
from typing import Any

from src.analysis.trader_discovery import DEFAULT_TRADER_DISCOVERY_DB, load_discovered_wallets
from src.analysis.trader_registry import DEFAULT_TRADERS_DB, list_traders, load_wallet_report
from src.intelligence.wallet_alpha_lab import WalletAlphaLab
from src.intelligence.wallet_synthetic_filter import is_synthetic_wallet
from src.sqlite_utils import closing_connection

LEADERBOARD_SOURCE = "polymarket_leaderboard"

STRATEGY_CLUSTERS = (
    "market_maker",
    "event_specialist",
    "momentum_trader",
    "arbitrageur",
    "sports_specialist",
    "crypto_specialist",
    "generalist",
    "unknown",
)


def _with_flags(payload: dict[str, Any]) -> dict[str, Any]:
    return {"read_only": True, "paper_only": True, "analytics_only": True, **payload}


def _safe_float(value: Any, default: float = 0.0) -> float:
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _load_leaderboard_discovered_wallets(
    discovery_db_path: str | Path = DEFAULT_TRADER_DISCOVERY_DB,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """Return leaderboard-sourced discovered wallets with metadata."""
    candidates = load_discovered_wallets(db_path=discovery_db_path, limit=limit or 10_000)
    return [
        {
            "wallet": candidate.wallet,
            "source": candidate.source,
            "discovery_score": candidate.discovery_score,
            "evidence_count": candidate.evidence_count,
            "markets_seen": list(candidate.markets_seen or []),
            "is_synthetic": is_synthetic_wallet(candidate.wallet),
        }
        for candidate in candidates
        if candidate.source == LEADERBOARD_SOURCE and not is_synthetic_wallet(candidate.wallet)
    ]


def _load_registry_status_for_wallets(
    wallets: list[str],
    traders_db_path: str | Path = DEFAULT_TRADERS_DB,
) -> dict[str, dict[str, Any]]:
    """Map normalized wallet -> registry status dict. Read-only."""
    result: dict[str, dict[str, Any]] = {}
    for wallet in wallets:
        normalized = str(wallet).strip().lower()
        record = load_wallet_report(normalized, db_path=str(traders_db_path))
        if record:
            try:
                report = json.loads(record.report_json or "{}")
            except json.JSONDecodeError:
                report = {}
            result[normalized] = {
                "status": "accepted",
                "classification": record.classification,
                "confidence": record.confidence,
                "watch_score": report.get("watch_score", 0),
            }
        else:
            result[normalized] = {"status": "not_registered", "classification": "unknown", "confidence": 0.0, "watch_score": 0}
    return result


def _alpha_scores_for_wallets(
    wallets: list[str],
    traders_db_path: str | Path = DEFAULT_TRADERS_DB,
) -> dict[str, dict[str, Any]]:
    """Compute alpha scores for a wallet list without persisting."""
    lab = WalletAlphaLab(traders_db_path=traders_db_path)
    result: dict[str, dict[str, Any]] = {}
    for wallet in wallets:
        normalized = str(wallet).strip().lower()
        if is_synthetic_wallet(normalized):
            continue
        try:
            report = lab.analyze_wallet(normalized)
            score = lab.compute_alpha_score(report)
            result[normalized] = {
                "alpha_score": score.alpha_score,
                "alpha_confidence": score.alpha_confidence,
                "alpha_grade": score.alpha_grade,
                "status": report.status,
            }
        except Exception:
            result[normalized] = {
                "alpha_score": 0.0,
                "alpha_confidence": 0.0,
                "alpha_grade": "F",
                "status": "insufficient_data",
            }
    return result


def leaderboard_alpha_rankings(
    *,
    traders_db_path: str | Path = DEFAULT_TRADERS_DB,
    discovery_db_path: str | Path = DEFAULT_TRADER_DISCOVERY_DB,
    limit: int = 25,
) -> dict[str, Any]:
    """Alpha rankings filtered to leaderboard-sourced real wallets only."""
    discovered = _load_leaderboard_discovered_wallets(discovery_db_path=discovery_db_path)
    wallets = [row["wallet"] for row in discovered]
    alpha_by_wallet = _alpha_scores_for_wallets(wallets, traders_db_path=traders_db_path)

    rankings: list[dict[str, Any]] = []
    for row in discovered:
        wallet = row["wallet"]
        alpha = alpha_by_wallet.get(wallet, {})
        rankings.append(
            {
                "wallet": wallet,
                "discovery_score": row["discovery_score"],
                "evidence_count": row["evidence_count"],
                "alpha_score": alpha.get("alpha_score", 0.0),
                "alpha_confidence": alpha.get("alpha_confidence", 0.0),
                "alpha_grade": alpha.get("alpha_grade", "F"),
                "alpha_status": alpha.get("status", "insufficient_data"),
                "source": LEADERBOARD_SOURCE,
            }
        )

    rankings.sort(key=lambda row: (-row["alpha_score"], -row["alpha_confidence"], row["wallet"]))
    for index, row in enumerate(rankings[:limit], start=1):
        row["alpha_rank"] = index

    return _with_flags(
        {
            "leaderboard_only": True,
            "real_wallet_only": True,
            "rankings_count": len(rankings),
            "rankings": rankings[:limit],
            "synthetic_wallet_count": 0,
        }
    )


def wallet_performance_breakdown(
    *,
    traders_db_path: str | Path = DEFAULT_TRADERS_DB,
    discovery_db_path: str | Path = DEFAULT_TRADER_DISCOVERY_DB,
    source: str = LEADERBOARD_SOURCE,
) -> dict[str, Any]:
    """Summarize leaderboard wallet performance across registry states."""
    candidates = load_discovered_wallets(db_path=discovery_db_path, limit=10_000)
    source_wallets = [
        candidate
        for candidate in candidates
        if candidate.source == source and not is_synthetic_wallet(candidate.wallet)
    ]

    registry_status = _load_registry_status_for_wallets(
        [candidate.wallet for candidate in source_wallets],
        traders_db_path=traders_db_path,
    )

    alpha_by_wallet = _alpha_scores_for_wallets(
        [candidate.wallet for candidate in source_wallets],
        traders_db_path=traders_db_path,
    )

    accepted: list[str] = []
    probation: list[str] = []
    rejected: list[str] = []
    not_registered: list[str] = []

    for candidate in source_wallets:
        status = registry_status.get(candidate.wallet, {}).get("status", "not_registered")
        if status == "accepted":
            accepted.append(candidate.wallet)
        elif status == "probation":
            probation.append(candidate.wallet)
        elif status == "rejected":
            rejected.append(candidate.wallet)
        else:
            not_registered.append(candidate.wallet)

    alpha_scores = [alpha_by_wallet.get(wallet, {}).get("alpha_score", 0.0) for wallet in accepted + probation]
    alpha_scores = [score for score in alpha_scores if score > 0]

    top_wallet = ""
    top_alpha_score = 0.0
    if alpha_scores:
        for wallet, alpha in alpha_by_wallet.items():
            if alpha.get("alpha_score", 0.0) > top_alpha_score:
                top_alpha_score = alpha.get("alpha_score", 0.0)
                top_wallet = wallet

    avg_alpha = round(statistics.mean(alpha_scores), 4) if alpha_scores else 0.0
    median_alpha = round(statistics.median(alpha_scores), 4) if alpha_scores else 0.0

    return _with_flags(
        {
            "source": source,
            "total_wallets": len(source_wallets),
            "accepted_wallets": len(accepted),
            "probation_wallets": len(probation),
            "rejected_wallets": len(rejected),
            "not_registered_wallets": len(not_registered),
            "synthetic_wallet_count": 0,
            "average_alpha_score": avg_alpha,
            "median_alpha_score": median_alpha,
            "top_wallet": top_wallet,
            "top_alpha_score": round(top_alpha_score, 4),
            "accepted": accepted[:10],
            "probation": probation[:10],
            "rejected": rejected[:10],
        }
    )


def wallet_follow_candidates(
    *,
    traders_db_path: str | Path = DEFAULT_TRADERS_DB,
    discovery_db_path: str | Path = DEFAULT_TRADER_DISCOVERY_DB,
    source: str = LEADERBOARD_SOURCE,
    limit: int = 10,
) -> dict[str, Any]:
    """Return top leaderboard-derived follow candidates with reasons."""
    discovered = _load_leaderboard_discovered_wallets(discovery_db_path=discovery_db_path)
    if not discovered:
        return _with_flags({"candidates": [], "count": 0, "source": source})

    wallet_order = [row["wallet"] for row in discovered]
    alpha_by_wallet = _alpha_scores_for_wallets(wallet_order, traders_db_path=traders_db_path)
    registry_status = _load_registry_status_for_wallets(wallet_order, traders_db_path=traders_db_path)

    candidates: list[dict[str, Any]] = []
    for row in discovered:
        wallet = row["wallet"]
        alpha = alpha_by_wallet.get(wallet, {})
        registry = registry_status.get(wallet, {})
        score = alpha.get("alpha_score", 0.0)
        confidence = alpha.get("alpha_confidence", 0.0)
        discovery_score = row["discovery_score"]

        reasons: list[str] = []
        if score >= 70:
            reasons.append("high_alpha_score")
        elif score >= 55:
            reasons.append("developing_alpha")
        if confidence >= 0.8:
            reasons.append("high_confidence")
        if discovery_score >= 90:
            reasons.append("top_leaderboard_rank")
        elif discovery_score >= 75:
            reasons.append("strong_leaderboard_rank")
        if registry.get("status") == "accepted":
            reasons.append("registry_accepted")
        elif registry.get("status") == "probation":
            reasons.append("under_probation")
        if not reasons:
            reasons.append("leaderboard_source")

        candidates.append(
            {
                "wallet": wallet,
                "label": row["wallet"],
                "alpha_score": round(score, 4),
                "confidence": round(confidence, 4),
                "discovery_score": discovery_score,
                "registry_status": registry.get("status", "not_registered"),
                "reason": ", ".join(reasons),
            }
        )

    candidates.sort(key=lambda row: (-row["alpha_score"], -row["confidence"], -row["discovery_score"], row["wallet"]))
    return _with_flags({"candidates": candidates[:limit], "count": len(candidates), "source": source})


def _classify_leaderboard_wallet(wallet: str, metadata: dict[str, Any]) -> str:
    """Lightweight analytics-only classifier using existing metadata."""
    category = str(metadata.get("category") or "").upper()
    order_by = str(metadata.get("order_by") or "").upper()
    rank = int(metadata.get("rank") or 0)
    pnl = _safe_float(metadata.get("pnl"))
    vol = _safe_float(metadata.get("vol"))
    user_name = str(metadata.get("user_name") or "").lower()

    # High-volume, low-PnL variance suggests market making / liquidity provision.
    if vol > 10_000_000 and abs(pnl) < vol * 0.05:
        return "market_maker"

    # Strong category specialization.
    if category in {"SPORTS", "POLITICS", "CRYPTO", "CULTURE", "ECONOMICS", "TECH"}:
        if category == "SPORTS":
            return "sports_specialist"
        if category == "CRYPTO":
            return "crypto_specialist"
        return "event_specialist"

    # High rank + PnL driven by volume ordering suggests momentum.
    if order_by == "VOL" and rank <= 10 and pnl > 0:
        return "momentum_trader"

    # PnL ordering with strong rank and balanced volume suggests arbitrage-style edge.
    if order_by == "PNL" and rank <= 10 and vol > 1_000_000:
        return "arbitrageur"

    # Username heuristics (weak signal, only used when no stronger signal exists).
    if any(token in user_name for token in ("arb", "mm", "maker", "liquidity")):
        return "arbitrageur" if "arb" in user_name else "market_maker"
    if any(token in user_name for token in ("sport", "politic", "crypto", "event")):
        if "sport" in user_name:
            return "sports_specialist"
        if "crypto" in user_name:
            return "crypto_specialist"
        return "event_specialist"

    if rank > 0:
        return "generalist"
    return "unknown"


def wallet_strategy_clustering(
    *,
    traders_db_path: str | Path = DEFAULT_TRADERS_DB,
    discovery_db_path: str | Path = DEFAULT_TRADER_DISCOVERY_DB,
    source: str = LEADERBOARD_SOURCE,
    leaderboard_only: bool = False,
    limit: int = 25,
) -> dict[str, Any]:
    """Cluster wallets by lightweight strategy classification."""
    candidates = load_discovered_wallets(db_path=discovery_db_path, limit=10_000)
    if leaderboard_only:
        candidates = [candidate for candidate in candidates if candidate.source == source]

    wallets = [candidate.wallet for candidate in candidates if not is_synthetic_wallet(candidate.wallet)]
    registry_status = _load_registry_status_for_wallets(wallets, traders_db_path=traders_db_path)
    alpha_by_wallet = _alpha_scores_for_wallets(wallets, traders_db_path=traders_db_path)

    # Load latest leaderboard metadata from traders_db if available.
    metadata_by_wallet: dict[str, dict[str, Any]] = {}
    try:
        with closing_connection(traders_db_path) as conn:
            rows = conn.execute(
                """
                SELECT wallet, metadata_json
                FROM polymarket_leaderboard_entries
                WHERE wallet IN ({placeholders})
                ORDER BY recorded_at DESC
                """.replace("{placeholders}", ",".join("?" * len(wallets))),
                wallets,
            ).fetchall()
        for row in rows:
            wallet = str(row["wallet"]).strip().lower()
            if wallet not in metadata_by_wallet:
                try:
                    metadata_by_wallet[wallet] = json.loads(row["metadata_json"] or "{}")
                except json.JSONDecodeError:
                    metadata_by_wallet[wallet] = {}
    except Exception:
        metadata_by_wallet = {}

    buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for candidate in candidates:
        wallet = candidate.wallet
        if is_synthetic_wallet(wallet):
            continue
        metadata = metadata_by_wallet.get(wallet, {})
        cluster = _classify_leaderboard_wallet(wallet, metadata)
        alpha = alpha_by_wallet.get(wallet, {})
        buckets[cluster].append(
            {
                "wallet": wallet,
                "alpha_score": alpha.get("alpha_score", 0.0),
                "alpha_confidence": alpha.get("alpha_confidence", 0.0),
                "discovery_score": candidate.discovery_score,
                "registry_status": registry_status.get(wallet, {}).get("status", "not_registered"),
                "metadata": metadata,
            }
        )

    summary: list[dict[str, Any]] = []
    for cluster in STRATEGY_CLUSTERS:
        group = buckets.get(cluster, [])
        if not group:
            continue
        alphas = [row["alpha_score"] for row in group if row["alpha_score"] > 0]
        avg_alpha = round(statistics.mean(alphas), 4) if alphas else 0.0
        top = sorted(
            group,
            key=lambda row: (-row["alpha_score"], -row["alpha_confidence"], -row["discovery_score"], row["wallet"]),
        )[:5]
        summary.append(
            {
                "category": cluster,
                "wallet_count": len(group),
                "average_alpha_score": avg_alpha,
                "top_wallets": [
                    {
                        "wallet": row["wallet"],
                        "alpha_score": round(row["alpha_score"], 4),
                        "confidence": round(row["alpha_confidence"], 4),
                        "discovery_score": row["discovery_score"],
                    }
                    for row in top
                ],
                "confidence_notes": "alpha_scores_computed_from_existing_signals_and_paper_copy" if alphas else "insufficient_alpha_data",
            }
        )

    summary.sort(key=lambda row: -row["average_alpha_score"])
    return _with_flags(
        {
            "leaderboard_only": leaderboard_only,
            "source": source,
            "clusters": summary[:limit],
            "total_classified": sum(row["wallet_count"] for row in summary),
            "synthetic_wallet_count": 0,
        }
    )
