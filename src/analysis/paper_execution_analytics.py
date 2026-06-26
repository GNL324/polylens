from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

from src.sqlite_utils import closing_connection


def execution_quality_report(db_path: str | Path) -> dict[str, Any]:
    path = Path(db_path)
    if not path.exists():
        return _empty_report()

    slippages: list[float] = []
    spreads: list[float] = []
    rejection_reasons: Counter[str] = Counter()
    filled = partial = blocked = attempted = 0

    with closing_connection(path) as conn:
        rows = conn.execute(
            """
            SELECT status, stake, raw_json
            FROM paper_orders
            ORDER BY id
            """
        ).fetchall()

    for row in rows:
        status = str(row["status"] or "")
        raw = _load_json(row["raw_json"])
        execution = raw.get("execution") if isinstance(raw.get("execution"), dict) else {}
        attempted += 1

        if status == "blocked":
            blocked += 1
            reason = str(raw.get("blocked_reason") or execution.get("reason") or "blocked")
            rejection_reasons[reason] += 1
            continue

        exec_status = str(execution.get("status") or status)
        if exec_status == "partial":
            partial += 1
        elif status == "filled":
            filled += 1

        if execution.get("slippage") is not None:
            slippages.append(float(execution["slippage"]))
        if execution.get("spread_paid") is not None:
            spreads.append(float(execution["spread_paid"]))
        if execution.get("reason"):
            rejection_reasons[str(execution["reason"])] += 1

    fill_attempts = filled + partial + blocked
    return {
        "read_only": True,
        "paper_only": True,
        "orders_seen": attempted,
        "filled_orders": filled,
        "partial_fills": partial,
        "blocked_orders": blocked,
        "fill_rate": round((filled + partial) / fill_attempts, 6) if fill_attempts else 0.0,
        "rejection_rate": round(blocked / fill_attempts, 6) if fill_attempts else 0.0,
        "partial_fill_rate": round(partial / fill_attempts, 6) if fill_attempts else 0.0,
        "average_slippage": round(sum(slippages) / len(slippages), 6) if slippages else 0.0,
        "average_spread_paid": round(sum(spreads) / len(spreads), 6) if spreads else 0.0,
        "top_rejection_reasons": [
            {"reason": reason, "count": count}
            for reason, count in rejection_reasons.most_common(5)
        ],
    }


def _empty_report() -> dict[str, Any]:
    return {
        "read_only": True,
        "paper_only": True,
        "orders_seen": 0,
        "filled_orders": 0,
        "partial_fills": 0,
        "blocked_orders": 0,
        "fill_rate": 0.0,
        "rejection_rate": 0.0,
        "partial_fill_rate": 0.0,
        "average_slippage": 0.0,
        "average_spread_paid": 0.0,
        "top_rejection_reasons": [],
    }


def _load_json(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    try:
        loaded = json.loads(value or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return loaded if isinstance(loaded, dict) else {}
