from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from src.analysis.trader_signal_validation import init_trader_signal_validation_db
from src.sqlite_utils import closing_connection

DEFAULT_TRADER_SIGNAL_DB = "data/trader_signals.db"
SIGNAL_TYPES = ("early_entry", "conviction", "exit", "consensus", "contrarian")

DEFAULT_MINIMUM_VALIDATIONS = 20
DEFAULT_MINIMUM_ACCURACY = 0.55
GATE_STATUSES = ("proven", "unproven", "weak", "blocked")


def _with_flags(payload: dict[str, Any]) -> dict[str, Any]:
    return {"read_only": True, "paper_only": True, **payload}


def _parse_timestamp(value: Any) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        pass
    iso = text.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except ValueError:
        return None


def _accuracy(rows: list[dict[str, Any]]) -> float | None:
    if not rows:
        return None
    correct = sum(int(row["correct"]) for row in rows)
    return round(correct / len(rows), 6)


def _avg_roi(rows: list[dict[str, Any]]) -> float | None:
    values = [float(row["roi_proxy"]) for row in rows if row.get("roi_proxy") is not None]
    if not values:
        return None
    return round(sum(values) / len(values), 6)


def _rolling_accuracy(rows: list[dict[str, Any]], days: int, now: datetime) -> float | None:
    cutoff = now - timedelta(days=days)
    window_rows = []
    for row in rows:
        resolved = _parse_timestamp(row.get("resolved_at"))
        if resolved is None:
            continue
        if datetime.fromtimestamp(resolved, tz=timezone.utc) >= cutoff:
            window_rows.append(row)
    return _accuracy(window_rows)


def _confidence_score(validation_count: int, accuracy: float | None, minimum_validations: int) -> float:
    if validation_count <= 0 or accuracy is None:
        return 0.0
    sample_factor = min(1.0, validation_count / max(minimum_validations, 1))
    return round(accuracy * sample_factor * 100.0, 4)


@dataclass(frozen=True)
class GateThresholds:
    minimum_validations: int = DEFAULT_MINIMUM_VALIDATIONS
    minimum_accuracy: float = DEFAULT_MINIMUM_ACCURACY


def evaluate_signal_family_gate(
    *,
    validation_count: int,
    accuracy: float | None,
    thresholds: GateThresholds | None = None,
) -> dict[str, Any]:
    thresholds = thresholds or GateThresholds()
    if validation_count < thresholds.minimum_validations:
        return {
            "gate_status": "unproven",
            "gate_passes": False,
            "gate_reason": f"validation_count {validation_count} below minimum {thresholds.minimum_validations}",
        }
    if accuracy is None or accuracy < thresholds.minimum_accuracy:
        return {
            "gate_status": "weak",
            "gate_passes": False,
            "gate_reason": f"accuracy {accuracy} below minimum {thresholds.minimum_accuracy}",
        }
    return {
        "gate_status": "proven",
        "gate_passes": True,
        "gate_reason": "signal family meets validation and accuracy thresholds",
    }


def load_signal_family_stats(
    *,
    db_path: str | Path = DEFAULT_TRADER_SIGNAL_DB,
    now: datetime | None = None,
) -> dict[str, dict[str, Any]]:
    init_trader_signal_validation_db(db_path)
    now = now or datetime.now(timezone.utc)
    with closing_connection(db_path) as conn:
        rows = conn.execute(
            """
            SELECT signal_type, resolved_at, correct, roi_proxy, confidence
            FROM trader_signal_validation
            ORDER BY signal_type ASC, resolved_at ASC
            """
        ).fetchall()

    grouped: dict[str, list[dict[str, Any]]] = {signal_type: [] for signal_type in SIGNAL_TYPES}
    for row in rows:
        signal_type = str(row["signal_type"] or "unknown")
        grouped.setdefault(signal_type, []).append(dict(row))

    stats: dict[str, dict[str, Any]] = {}
    for signal_type in SIGNAL_TYPES:
        family_rows = grouped.get(signal_type, [])
        accuracy = _accuracy(family_rows)
        validation_count = len(family_rows)
        stats[signal_type] = {
            "signal_type": signal_type,
            "validation_count": validation_count,
            "accuracy": accuracy,
            "avg_roi_proxy": _avg_roi(family_rows),
            "rolling_7_day_accuracy": _rolling_accuracy(family_rows, 7, now),
            "rolling_30_day_accuracy": _rolling_accuracy(family_rows, 30, now),
            "confidence_score": _confidence_score(validation_count, accuracy, DEFAULT_MINIMUM_VALIDATIONS),
        }
        stats[signal_type].update(
            evaluate_signal_family_gate(validation_count=validation_count, accuracy=accuracy)
        )
    return stats


def apply_gate_to_recommendation(
    recommendation: dict[str, Any],
    family_stats: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    signal_type = str(recommendation.get("signal_type") or "unknown")
    gate = family_stats.get(signal_type) or {
        "validation_count": 0,
        "accuracy": None,
        "gate_status": "unproven",
        "gate_passes": False,
        "gate_reason": "no validation history for signal family",
    }
    enriched = {
        **recommendation,
        "validation_count": int(gate.get("validation_count") or 0),
        "historical_accuracy": gate.get("accuracy"),
        "gate_status": gate.get("gate_status"),
        "gate_reason": gate.get("gate_reason"),
        "confidence_score": gate.get("confidence_score", 0.0),
    }
    if not gate.get("gate_passes"):
        enriched["recommendation_type"] = "blocked"
        enriched["reason"] = f"blocked: {gate.get('gate_reason')}"
    return enriched


def trader_signal_gates_report(
    *,
    db_path: str | Path = DEFAULT_TRADER_SIGNAL_DB,
    thresholds: GateThresholds | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    thresholds = thresholds or GateThresholds()
    family_stats = load_signal_family_stats(db_path=db_path, now=now)
    proven = [stats for stats in family_stats.values() if stats["gate_status"] == "proven"]
    blocked = [stats for stats in family_stats.values() if stats["gate_status"] in {"unproven", "weak"}]
    return _with_flags(
        {
            "recommended_thresholds": {
                "minimum_validations": thresholds.minimum_validations,
                "minimum_accuracy": thresholds.minimum_accuracy,
            },
            "signal_families": list(family_stats.values()),
            "proven_signal_families": proven,
            "blocked_signal_families": blocked,
            "gate_summary": {
                "proven_count": len(proven),
                "blocked_count": len(blocked),
                "unproven_count": sum(1 for stats in family_stats.values() if stats["gate_status"] == "unproven"),
                "weak_count": sum(1 for stats in family_stats.values() if stats["gate_status"] == "weak"),
            },
        }
    )


def split_recommendations_by_gate(recommendations: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    promoted = [
        item
        for item in recommendations
        if item.get("gate_status") == "proven" and item.get("recommendation_type") != "blocked"
    ]
    blocked = [
        item
        for item in recommendations
        if item.get("recommendation_type") == "blocked" or item.get("gate_status") in {"unproven", "weak"}
    ]
    return {"promoted_recommendations": promoted, "blocked_recommendations": blocked}
