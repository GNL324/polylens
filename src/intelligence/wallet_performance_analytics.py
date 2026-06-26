from __future__ import annotations

import json
import statistics
from collections import Counter
from pathlib import Path
from typing import Any

from src.analysis.paper_copy_trader import DEFAULT_PAPER_COPY_DB
from src.analysis.trader_discovery import DEFAULT_TRADER_DISCOVERY_DB
from src.analysis.trader_registry import DEFAULT_TRADERS_DB
from src.analysis.trader_signal_engine import DEFAULT_TRADER_SIGNAL_DB
from src.intelligence.wallet_performance import WalletPerformanceEngine, init_wallet_performance_db
from src.intelligence.wallet_scoring import WalletScorer
from src.intelligence.wallet_signal_analytics import wallet_signal_analytics_report
from src.sqlite_utils import closing_connection


def _with_flags(payload: dict[str, Any]) -> dict[str, Any]:
    return {"read_only": True, "paper_only": True, **payload}


def _distribution(values: list[float], *, buckets: int = 5) -> dict[str, int]:
    if not values:
        return {}
    minimum = min(values)
    maximum = max(values)
    if minimum == maximum:
        return {f"{minimum:.2f}": len(values)}
    step = (maximum - minimum) / buckets
    counts: Counter[str] = Counter()
    for value in values:
        index = min(buckets - 1, int((value - minimum) / step) if step > 0 else 0)
        low = minimum + index * step
        high = low + step
        counts[f"{low:.2f}-{high:.2f}"] += 1
    return dict(counts)


def wallet_performance_analytics_report(
    *,
    traders_db_path: str | Path = DEFAULT_TRADERS_DB,
    discovery_db_path: str | Path = DEFAULT_TRADER_DISCOVERY_DB,
    signal_db_path: str | Path = DEFAULT_TRADER_SIGNAL_DB,
    paper_copy_db_path: str | Path = DEFAULT_PAPER_COPY_DB,
    top_limit: int = 15,
) -> dict[str, Any]:
    init_wallet_performance_db(traders_db_path)
    engine = WalletPerformanceEngine(
        traders_db_path=traders_db_path,
        discovery_db_path=discovery_db_path,
        signal_db_path=signal_db_path,
        paper_copy_db_path=paper_copy_db_path,
    )
    scorer = WalletScorer(
        traders_db_path=traders_db_path,
        discovery_db_path=discovery_db_path,
        signal_db_path=signal_db_path,
        paper_copy_db_path=paper_copy_db_path,
    )
    signal_analytics = wallet_signal_analytics_report(
        traders_db_path=traders_db_path,
        signal_db_path=signal_db_path,
        paper_copy_db_path=paper_copy_db_path,
        wallet_limit=top_limit * 2,
    )

    snapshots = engine.load_snapshots(limit=500)
    latest_by_wallet: dict[str, dict[str, Any]] = {}
    for row in snapshots:
        wallet = str(row["wallet"])
        if wallet not in latest_by_wallet:
            latest_by_wallet[wallet] = row

    roi_values: list[float] = []
    expectancy_values: list[float] = []
    for row in latest_by_wallet.values():
        metrics = row.get("metrics") or {}
        roi = metrics.get("realized_roi")
        if roi is not None:
            roi_values.append(float(roi))
        expectancy = metrics.get("expectancy")
        if expectancy is not None:
            expectancy_values.append(float(expectancy))

    status_counts = Counter(str(row.get("status") or "unknown") for row in latest_by_wallet.values())
    promoted = [row for row in latest_by_wallet.values() if row.get("status") == "promoted"]
    retired = [row for row in latest_by_wallet.values() if row.get("status") == "retired"]

    promoted_avg_score = round(statistics.mean(row["score"] for row in promoted), 4) if promoted else 0.0
    retired_avg_score = round(statistics.mean(row["score"] for row in retired), 4) if retired else 0.0

    score_drift: list[dict[str, Any]] = []
    with closing_connection(traders_db_path) as conn:
        trend_rows = conn.execute(
            """
            SELECT wallet, score_delta, previous_score, current_score, trend, recorded_at
            FROM wallet_performance_trends
            ORDER BY recorded_at DESC, id DESC
            LIMIT ?
            """,
            (top_limit * 3,),
        ).fetchall()
        score_drift = [dict(row) for row in trend_rows]

    signal_decay: list[dict[str, Any]] = []
    for wallet, row in list(latest_by_wallet.items())[:top_limit * 2]:
        metrics = row.get("metrics") or {}
        components = row.get("components") or {}
        freshness = components.get("signal_freshness", 0.0)
        frequency = metrics.get("signal_frequency", 0)
        if freshness < 40 or (frequency and freshness / max(1, frequency) < 5):
            signal_decay.append(
                {
                    "wallet": wallet,
                    "signal_freshness": freshness,
                    "signal_frequency": frequency,
                    "trend": row.get("trend"),
                    "score": row.get("score"),
                }
            )

    archetype_rows = signal_analytics.get("archetype_performance", [])
    wallets = list(latest_by_wallet.keys())[: top_limit * 2]
    ranked = scorer.rank_wallets(wallets) if wallets else []
    top_performers = sorted(latest_by_wallet.values(), key=lambda r: -float(r["score"]))[:top_limit]

    actions = engine.load_actions(limit=top_limit * 2)
    recent_promotions = [row for row in actions if row.get("action") == "promoted"][:top_limit]
    recent_demotions = [row for row in actions if row.get("action") == "demoted"][:top_limit]
    recent_retirements = [row for row in actions if row.get("action") == "retired"][:top_limit]

    return _with_flags(
        {
            "wallet_count": len(latest_by_wallet),
            "status_distribution": dict(status_counts),
            "roi_distribution": _distribution(roi_values),
            "expectancy_distribution": _distribution(expectancy_values),
            "archetype_performance": archetype_rows[:top_limit],
            "promoted_wallet_performance": {
                "count": len(promoted),
                "avg_score": promoted_avg_score,
                "wallets": promoted[:top_limit],
            },
            "retired_wallet_performance": {
                "count": len(retired),
                "avg_score": retired_avg_score,
                "wallets": retired[:top_limit],
            },
            "score_drift": score_drift[:top_limit],
            "signal_decay": signal_decay[:top_limit],
            "top_performers": top_performers,
            "recent_promotions": recent_promotions,
            "recent_demotions": recent_demotions,
            "recent_retirements": recent_retirements,
            "ranked_scores": [row.to_dict() for row in ranked[:top_limit]],
        }
    )
