from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

from src.analysis.trader_discovery import DEFAULT_TRADER_DISCOVERY_DB
from src.analysis.trader_registry import DEFAULT_TRADERS_DB
from src.intelligence.wallet_discovery import WalletDiscoveryEngine, init_wallet_discovery_db
from src.intelligence.wallet_scoring import WalletScorer
from src.sqlite_utils import closing_connection


def _with_flags(payload: dict[str, Any]) -> dict[str, Any]:
    return {"read_only": True, "paper_only": True, **payload}


def wallet_discovery_analytics_report(
    *,
    traders_db_path: str | Path = DEFAULT_TRADERS_DB,
    discovery_db_path: str | Path = DEFAULT_TRADER_DISCOVERY_DB,
    top_limit: int = 10,
) -> dict[str, Any]:
    init_wallet_discovery_db(traders_db_path, discovery_db_path)
    engine = WalletDiscoveryEngine(traders_db_path=traders_db_path, discovery_db_path=discovery_db_path)
    scorer = WalletScorer(traders_db_path=traders_db_path, discovery_db_path=discovery_db_path)

    discovery_runs = 0
    wallets_found_total = 0
    new_wallets_total = 0
    with closing_connection(discovery_db_path) as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) AS runs,
                   COALESCE(SUM(wallets_found), 0) AS wallets_found,
                   COALESCE(SUM(new_wallets), 0) AS new_wallets
            FROM discovery_runs
            """
        ).fetchone()
        if row:
            discovery_runs = int(row["runs"] or 0)
            wallets_found_total = int(row["wallets_found"] or 0)
            new_wallets_total = int(row["new_wallets"] or 0)

    lifecycle_rows = engine.load_lifecycle(limit=500)
    state_counts = Counter(str(row.get("state") or "unknown") for row in lifecycle_rows)
    active_count = int(state_counts.get("active", 0))
    retired_count = int(state_counts.get("retired", 0))
    survival_rate = round(active_count / max(1, active_count + retired_count), 4)

    score_distribution: dict[str, int] = {"elite": 0, "strong": 0, "developing": 0, "weak": 0, "unscored": 0}
    for row in lifecycle_rows:
        category = str(row.get("category") or "unscored")
        if category in score_distribution:
            score_distribution[category] += 1
        else:
            score_distribution["unscored"] += 1

    archetype_distribution: Counter[str] = Counter()
    with closing_connection(traders_db_path) as conn:
        tables = {row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "strategy_profiles" in tables:
            for row in conn.execute("SELECT archetype FROM strategy_profiles").fetchall():
                archetype_distribution[str(row["archetype"])] += 1

    wallets = [str(row["wallet"]) for row in lifecycle_rows[: max(top_limit * 3, 30)]]
    ranked = scorer.rank_wallets(wallets) if wallets else []
    top_performers = [row.to_dict() for row in ranked[:top_limit]]
    worst_performers = [row.to_dict() for row in sorted(ranked, key=lambda r: (r.score, r.wallet))[:top_limit]]

    discovery_rate = round(wallets_found_total / max(1, discovery_runs), 4) if discovery_runs else 0.0

    return _with_flags(
        {
            "discovery_rate": discovery_rate,
            "discovery_runs": discovery_runs,
            "new_wallets_total": new_wallets_total,
            "survival_rate": survival_rate,
            "state_distribution": dict(state_counts),
            "score_distribution": score_distribution,
            "archetype_distribution": dict(archetype_distribution),
            "top_performers": top_performers,
            "worst_performers": worst_performers,
            "recent_discoveries": engine.load_recent_discoveries(limit=top_limit),
            "recent_promotions": [
                event for event in engine.load_promotion_events(limit=top_limit * 2)
                if event.get("new_state") in {"active", "watchlist"}
            ][:top_limit],
        }
    )
