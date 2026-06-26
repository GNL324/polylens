from __future__ import annotations

from typing import Any


def confidence_adjustment(metric: dict[str, Any]) -> dict[str, Any]:
    """Suggest, but do not apply, an adjusted confidence for one cohort."""
    current = _probability(metric.get("avg_confidence"))
    observed = _probability(metric.get("observed_accuracy") or metric.get("accuracy"))
    return {
        "label": metric.get("label") or "unknown",
        "sample_size": int(metric.get("resolved_count") or metric.get("total") or 0),
        "current_confidence": round(current, 6),
        "adjusted_confidence": round(observed, 6),
        "delta": round(observed - current, 6),
        "reason": "historical calibration",
        "auto_apply": False,
    }


def confidence_adjustments(grouped_metrics: dict[str, dict[str, dict[str, Any]]], *, limit: int = 5) -> dict[str, list[dict[str, Any]]]:
    recommendations: dict[str, list[dict[str, Any]]] = {}
    for dimension, rows in grouped_metrics.items():
        ranked = [
            confidence_adjustment(metric)
            for metric in rows.values()
            if int(metric.get("resolved_count") or 0) > 0
        ]
        ranked.sort(key=lambda item: (abs(float(item["delta"])), item["sample_size"]), reverse=True)
        recommendations[dimension] = ranked[: max(0, int(limit))]
    return recommendations


def _probability(value: Any) -> float:
    try:
        number = float(value or 0.0)
    except (TypeError, ValueError):
        number = 0.0
    if number > 1.0:
        number /= 100.0
    return min(1.0, max(0.0, number))
