from __future__ import annotations

import statistics
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any

from src.analysis.paper_copy_trader import DEFAULT_PAPER_COPY_DB
from src.analysis.trader_registry import DEFAULT_TRADERS_DB
from src.intelligence.wallet_signal_analytics import _load_strategy_profiles
from src.sqlite_utils import closing_connection


@dataclass
class DecayReport:
    wallet: str
    alpha_decay: float
    signal_half_life_days: float
    performance_persistence: float
    time_to_degradation_days: float
    decay_rate: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _with_flags(payload: dict[str, Any]) -> dict[str, Any]:
    return {"read_only": True, "paper_only": True, **payload}


def _safe_float(value: Any, default: float = 0.0) -> float:
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _parse_ts(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def wallet_decay_report(
    *,
    traders_db_path: str = DEFAULT_TRADERS_DB,
    paper_copy_db_path: str = DEFAULT_PAPER_COPY_DB,
    signal_db_path: str = "data/trader_signals.db",
) -> dict[str, Any]:
    _ = paper_copy_db_path, signal_db_path
    profiles = _load_strategy_profiles(traders_db_path)
    wallet_reports: list[DecayReport] = []
    archetype_decay: dict[str, list[float]] = {}

    with closing_connection(traders_db_path) as conn:
        tables = {row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "wallet_performance_trends" not in tables:
            return _with_flags({"wallets": [], "avg_half_life_days": None, "avg_decay_rate": None, "by_archetype": {}})
        trend_rows = conn.execute(
            """
            SELECT wallet, score_delta, previous_score, current_score, trend, recorded_at
            FROM wallet_performance_trends
            ORDER BY recorded_at ASC
            """
        ).fetchall()
        snapshot_rows = conn.execute(
            """
            SELECT wallet, score, components_json, recorded_at
            FROM wallet_performance_snapshots
            ORDER BY recorded_at ASC
            """
        ).fetchall()

    trends_by_wallet: dict[str, list[dict[str, Any]]] = {}
    for row in trend_rows:
        trends_by_wallet.setdefault(str(row["wallet"]), []).append(dict(row))

    snapshots_by_wallet: dict[str, list[dict[str, Any]]] = {}
    for row in snapshot_rows:
        snapshots_by_wallet.setdefault(str(row["wallet"]), []).append(dict(row))

    for wallet, trends in trends_by_wallet.items():
        if not trends:
            continue
        deltas = [_safe_float(t.get("score_delta")) for t in trends]
        negative_deltas = [abs(d) for d in deltas if d < 0]
        alpha_decay = statistics.mean(negative_deltas) if negative_deltas else 0.0
        decay_rate = len(negative_deltas) / max(1, len(deltas))

        timestamps = [_parse_ts(t.get("recorded_at")) for t in trends]
        timestamps = [t for t in timestamps if t is not None]
        half_life = 30.0
        if len(timestamps) >= 2:
            span_days = max(1.0, (timestamps[-1] - timestamps[0]).total_seconds() / 86_400.0)
            half_life = max(1.0, span_days / max(1, len(negative_deltas)))

        persistence = 1.0 - decay_rate
        degradation_days = half_life * 2 if decay_rate > 0.3 else 90.0

        report = DecayReport(
            wallet=wallet,
            alpha_decay=round(alpha_decay, 4),
            signal_half_life_days=round(half_life, 2),
            performance_persistence=round(persistence, 4),
            time_to_degradation_days=round(degradation_days, 2),
            decay_rate=round(decay_rate, 4),
        )
        wallet_reports.append(report)
        archetype = profiles.get(wallet, {}).get("archetype", "unknown")
        archetype_decay.setdefault(archetype, []).append(decay_rate)

    by_archetype = {
        archetype: {
            "avg_decay_rate": round(statistics.mean(rates), 4),
            "wallet_count": len(rates),
        }
        for archetype, rates in archetype_decay.items()
        if rates
    }

    half_lives = [r.signal_half_life_days for r in wallet_reports]
    decay_rates = [r.decay_rate for r in wallet_reports]

    return _with_flags(
        {
            "wallets": [report.to_dict() for report in wallet_reports],
            "avg_half_life_days": round(statistics.mean(half_lives), 2) if half_lives else None,
            "avg_decay_rate": round(statistics.mean(decay_rates), 4) if decay_rates else None,
            "by_archetype": by_archetype,
        }
    )
