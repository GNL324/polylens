from __future__ import annotations

from typing import Any

from src.analysis.short_crypto_paper import DEFAULT_DB_PATH
from src.analysis.strategy_feedback import MAX_ADJUSTMENT, MIN_TRADES, strategy_feedback_report


def strategy_recommendations_report(
    db_path: str = DEFAULT_DB_PATH,
    *,
    min_trades: int = MIN_TRADES,
    max_adjustment: float = MAX_ADJUSTMENT,
) -> dict[str, Any]:
    feedback = strategy_feedback_report(
        db_path,
        min_trades=min_trades,
        max_adjustment=max_adjustment,
    )
    recommendations = [_recommendation_from_strategy(row) for row in feedback.get("strategies", [])]
    return {
        "status": feedback.get("status", "ok"),
        "db_path": feedback.get("db_path", db_path),
        "min_trades": feedback.get("min_trades", min_trades),
        "max_adjustment": feedback.get("max_adjustment", max_adjustment),
        "recommendations": recommendations,
    }


def _recommendation_from_strategy(row: dict[str, Any]) -> dict[str, Any]:
    closed_trades = int(row.get("closed_trades") or 0)
    eligible = bool(row.get("eligible"))
    score_adjustment = float(row.get("score_adjustment") or 0.0)
    if not eligible:
        recommendation = "hold"
        confidence = "low"
        reason = row.get("reason") or "insufficient closed trades"
    elif score_adjustment > 0:
        recommendation = "increase_trust"
        confidence = _confidence(closed_trades)
        reason = "positive ROI over sufficient sample size"
    elif score_adjustment < 0:
        recommendation = "decrease_trust"
        confidence = _confidence(closed_trades)
        reason = "negative ROI over sufficient sample size"
    else:
        recommendation = "hold"
        confidence = _confidence(closed_trades)
        reason = "flat ROI over sufficient sample size"

    return {
        "strategy_label": row.get("strategy_label"),
        "closed_trades": closed_trades,
        "avg_roi": float(row.get("avg_roi") or 0.0),
        "score_adjustment": score_adjustment,
        "recommendation": recommendation,
        "confidence": confidence,
        "reason": reason,
    }


def _confidence(closed_trades: int) -> str:
    if closed_trades >= 100:
        return "high"
    if closed_trades >= 25:
        return "medium"
    return "low"
