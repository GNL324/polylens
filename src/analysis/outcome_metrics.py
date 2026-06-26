from __future__ import annotations

import math
from collections import defaultdict
from statistics import mean
from typing import Any, Iterable


DEFAULT_CONFIDENCE_BUCKETS = tuple(i / 10 for i in range(0, 11))


def brier_score(confidence: Any, outcome: Any) -> float:
    """Return the binary/proportional Brier score for one prediction."""
    probability = _probability(confidence)
    actual = _probability(outcome)
    return round((probability - actual) ** 2, 6)


def calibration_curve(rows: Iterable[dict[str, Any]], *, bucket_size: float = 0.1) -> list[dict[str, Any]]:
    """Bucket predictions by confidence and compare forecast vs observed accuracy."""
    buckets: dict[float, list[dict[str, float]]] = defaultdict(list)
    bucket_size = max(float(bucket_size or 0.1), 0.01)
    for row in rows:
        if not _is_resolved(row):
            continue
        confidence = _probability(row.get("confidence_at_entry"))
        observed = _correctness_score(row)
        bucket_index = math.floor((confidence + 1e-12) / bucket_size)
        bucket = min(1.0, bucket_index * bucket_size)
        buckets[round(bucket, 6)].append({"confidence": confidence, "observed": observed})
    curve: list[dict[str, Any]] = []
    for bucket, items in sorted(buckets.items()):
        avg_confidence = mean(item["confidence"] for item in items)
        observed_accuracy = mean(item["observed"] for item in items)
        curve.append(
            {
                "bucket": round(bucket, 6),
                "count": len(items),
                "avg_confidence": round(avg_confidence, 6),
                "observed_accuracy": round(observed_accuracy, 6),
                "confidence_bias": round(avg_confidence - observed_accuracy, 6),
            }
        )
    return curve


def validation_metrics(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    records = [row for row in rows]
    resolved = [row for row in records if _is_resolved(row)]
    correct = [row for row in records if str(row.get("validation_status") or "").lower() == "correct"]
    incorrect = [row for row in records if str(row.get("validation_status") or "").lower() == "incorrect"]
    partial = [row for row in records if str(row.get("validation_status") or "").lower() == "partial"]
    unresolved = [row for row in records if str(row.get("validation_status") or "").lower() == "unresolved"]
    score_sum = sum(_correctness_score(row) for row in resolved)
    briers = [_float(row.get("brier_score")) for row in resolved if row.get("brier_score") is not None]
    confidence_biases = [_probability(row.get("confidence_at_entry")) - _correctness_score(row) for row in resolved]
    accuracy = _ratio(score_sum, len(resolved))
    return {
        "total": len(records),
        "resolved_count": len(resolved),
        "correct_count": len(correct),
        "incorrect_count": len(incorrect),
        "partial_count": len(partial),
        "unresolved_count": len(unresolved),
        "accuracy": round(accuracy, 6),
        "precision": round(accuracy, 6),
        "recall": round(accuracy, 6),
        "brier_score": round(mean(briers), 6) if briers else 0.0,
        "confidence_bias": round(mean(confidence_biases), 6) if confidence_biases else 0.0,
        "calibration_drift": round(abs(mean(confidence_biases)), 6) if confidence_biases else 0.0,
        "avg_confidence": round(mean(_probability(row.get("confidence_at_entry")) for row in resolved), 6) if resolved else 0.0,
        "observed_accuracy": round(accuracy, 6),
        "calibration_curve": calibration_curve(resolved),
    }


def grouped_validation_metrics(rows: Iterable[dict[str, Any]], key: str) -> dict[str, dict[str, Any]]:
    buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        label = str(row.get(key) or "unknown")
        buckets[label].append(row)
    return {
        label: {"label": label, key: label, **validation_metrics(items)}
        for label, items in sorted(buckets.items())
    }


def rolling_group_metrics(
    rows: Iterable[dict[str, Any]],
    *,
    dimensions: tuple[str, ...] = ("copied_wallet", "strategy", "signal_family", "market_category", "expiry_bucket"),
) -> dict[str, dict[str, dict[str, Any]]]:
    records = [row for row in rows]
    return {dimension: grouped_validation_metrics(records, dimension) for dimension in dimensions}


def _is_resolved(row: dict[str, Any]) -> bool:
    return str(row.get("validation_status") or "").lower() in {"correct", "incorrect", "partial"}


def _correctness_score(row: dict[str, Any]) -> float:
    if row.get("correctness_score") is not None:
        return _probability(row.get("correctness_score"))
    status = str(row.get("validation_status") or "").lower()
    if status == "correct":
        return 1.0
    if status == "partial":
        return 0.5
    return 0.0


def _probability(value: Any) -> float:
    number = _float(value)
    if number > 1.0:
        number /= 100.0
    return min(1.0, max(0.0, number))


def _ratio(numerator: float, denominator: float) -> float:
    return 0.0 if denominator == 0 else numerator / denominator


def _float(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0
