from __future__ import annotations

from typing import Any

from src.analysis.short_crypto_paper import DEFAULT_DB_PATH, performance_report, settle_due
from src.analysis.strategy_feedback import MAX_ADJUSTMENT, MIN_TRADES, strategy_feedback_report


def short_crypto_feedback_loop(
    db_path: str = DEFAULT_DB_PATH,
    *,
    min_trades: int = MIN_TRADES,
    max_adjustment: float = MAX_ADJUSTMENT,
) -> dict[str, Any]:
    settlement = settle_due(db_path)
    paper_report = performance_report(db_path)
    feedback = strategy_feedback_report(
        db_path,
        min_trades=min_trades,
        max_adjustment=max_adjustment,
    )
    status = "ok" if feedback.get("status") == "ok" else str(feedback.get("status") or "ok")
    return {
        "status": status,
        "db_path": db_path,
        "settlement": settlement,
        "paper_report": paper_report,
        "strategy_feedback": feedback,
    }
