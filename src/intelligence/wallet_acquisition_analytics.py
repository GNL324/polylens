from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from src.analysis.trader_discovery import DEFAULT_TRADER_DISCOVERY_DB
from src.analysis.trader_registry import DEFAULT_TRADERS_DB, list_traders
from src.intelligence.wallet_data_acquisition import init_wallet_acquisition_db
from src.intelligence.real_wallet_ingestion import synthetic_rejection_stats
from src.intelligence.wallet_synthetic_filter import partition_wallets
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


def wallet_acquisition_analytics_report(
    *,
    traders_db_path: str | Path | None = None,
    discovery_db_path: str | Path | None = None,
    days: int = 7,
) -> dict[str, Any]:
    traders_db_path = Path(traders_db_path or DEFAULT_TRADERS_DB)
    discovery_db_path = Path(discovery_db_path or DEFAULT_TRADER_DISCOVERY_DB)
    init_wallet_acquisition_db(traders_db_path, discovery_db_path)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=max(int(days), 1))).replace(microsecond=0).isoformat().replace("+00:00", "Z")

    with closing_connection(traders_db_path) as conn:
        daily_rows = conn.execute(
            """
            SELECT DATE(recorded_at) AS day, COUNT(*) AS count
            FROM wallet_acquisition_records
            WHERE recorded_at >= ?
            GROUP BY DATE(recorded_at)
            ORDER BY day ASC
            """,
            (cutoff,),
        ).fetchall()
        status_rows = conn.execute(
            """
            SELECT status, COUNT(*) AS count
            FROM wallet_acquisition_records
            WHERE recorded_at >= ?
            GROUP BY status
            """,
            (cutoff,),
        ).fetchall()
        source_rows = conn.execute(
            """
            SELECT source, status, COUNT(*) AS count
            FROM wallet_acquisition_records
            WHERE recorded_at >= ?
            GROUP BY source, status
            """,
            (cutoff,),
        ).fetchall()
        quality_rows = conn.execute(
            """
            SELECT quality_score
            FROM wallet_acquisition_records
            WHERE recorded_at >= ?
            """,
            (cutoff,),
        ).fetchall()
        run_rows = conn.execute(
            """
            SELECT discovered, accepted, rejected, finished_at
            FROM wallet_acquisition_runs
            WHERE finished_at >= ?
            ORDER BY finished_at DESC
            LIMIT 50
            """,
            (cutoff,),
        ).fetchall()

    registry_count = len(list_traders(limit=10_000, db_path=str(traders_db_path)))
    real_wallets, synthetic_wallets = partition_wallets([row.wallet for row in list_traders(limit=10_000, db_path=str(traders_db_path))])
    synthetic_stats = synthetic_rejection_stats(traders_db_path=traders_db_path, days=days)
    wallets_discovered_per_day = {str(row["day"]): int(row["count"]) for row in daily_rows}
    status_counts = {str(row["status"]): int(row["count"]) for row in status_rows}
    source_effectiveness: dict[str, dict[str, int]] = {}
    for row in source_rows:
        source = str(row["source"])
        source_effectiveness.setdefault(source, {})
        source_effectiveness[source][str(row["status"])] = int(row["count"])

    quality_scores = [float(row["quality_score"]) for row in quality_rows]
    total_discovered = sum(int(row["discovered"]) for row in run_rows)
    total_accepted = sum(int(row["accepted"]) for row in run_rows)
    velocity = round(total_accepted / max(1, len(run_rows)), 4) if run_rows else 0.0

    return _with_flags(
        {
            "days": days,
            "wallets_discovered_per_day": wallets_discovered_per_day,
            "wallets_accepted": int(status_counts.get("accepted", 0)),
            "wallets_rejected": int(status_counts.get("rejected", 0)),
            "wallets_probation": int(status_counts.get("probation", 0)),
            "source_effectiveness": source_effectiveness,
            "quality_score_distribution": _distribution(quality_scores),
            "registry_growth": registry_count,
            "real_wallet_count": len(set(real_wallets)),
            "synthetic_wallet_count": len(set(synthetic_wallets)),
            "synthetic_rejections": synthetic_stats.get("synthetic_rejections", 0),
            "synthetic_rejections_by_source": synthetic_stats.get("by_source", {}),
            "acquisition_velocity": velocity,
            "recent_runs": [dict(row) for row in run_rows[:10]],
        }
    )


def wallet_acquisition_health_summary(
    *,
    traders_db_path: str | Path | None = None,
) -> dict[str, Any]:
    traders_db_path = Path(traders_db_path or DEFAULT_TRADERS_DB)
    init_wallet_acquisition_db(traders_db_path)
    with closing_connection(traders_db_path) as conn:
        last_run = conn.execute(
            "SELECT finished_at, accepted, rejected, discovered FROM wallet_acquisition_runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        failures = conn.execute(
            """
            SELECT COUNT(*) AS count FROM wallet_acquisition_records
            WHERE status = 'rejected' AND recorded_at >= datetime('now', '-1 day')
            """
        ).fetchone()
    status = "healthy"
    if last_run is None:
        status = "idle"
    elif int(last_run["discovered"] or 0) > 0 and int(last_run["accepted"] or 0) == 0:
        status = "degraded"
    return _with_flags(
        {
            "status": status,
            "last_run_at": last_run["finished_at"] if last_run else None,
            "last_discovered": int(last_run["discovered"] or 0) if last_run else 0,
            "last_accepted": int(last_run["accepted"] or 0) if last_run else 0,
            "rejections_24h": int(failures["count"] or 0) if failures else 0,
        }
    )


def wallet_registry_growth_report(
    *,
    traders_db_path: str | Path | None = None,
    limit: int = 30,
) -> dict[str, Any]:
    traders_db_path = Path(traders_db_path or DEFAULT_TRADERS_DB)
    rows = list_traders(limit=limit, db_path=str(traders_db_path))
    return _with_flags(
        {
            "registry_count": len(list_traders(limit=10_000, db_path=str(traders_db_path))),
            "recent_wallets": [row.to_dict() for row in rows],
        }
    )


def wallet_source_stats_report(
    *,
    traders_db_path: str | Path | None = None,
    days: int = 7,
) -> dict[str, Any]:
    analytics = wallet_acquisition_analytics_report(traders_db_path=traders_db_path, days=days)
    return _with_flags(
        {
            "source_effectiveness": analytics.get("source_effectiveness", {}),
            "wallets_discovered_per_day": analytics.get("wallets_discovered_per_day", {}),
            "real_wallet_count": analytics.get("real_wallet_count", 0),
            "synthetic_wallet_count": analytics.get("synthetic_wallet_count", 0),
            "synthetic_rejections": analytics.get("synthetic_rejections", 0),
            "synthetic_rejections_by_source": analytics.get("synthetic_rejections_by_source", {}),
        }
    )
