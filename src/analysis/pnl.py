from __future__ import annotations

from typing import Any


def summarize_pnl(positions: list[dict[str, Any]], trades: list[dict[str, Any]]) -> dict[str, Any]:
    exposure = sum(float(position.get("currentValue") or position.get("initialValue") or 0) for position in positions)
    estimated_cash_pnl = sum(float(position.get("cashPnl") or 0) for position in positions)
    realized_pnl = sum(float(position.get("realizedPnl") or 0) for position in positions)
    sizes = [float(position.get("currentValue") or position.get("initialValue") or 0) for position in positions]
    return {
        "estimated_pnl": round(estimated_cash_pnl + realized_pnl, 2),
        "cash_pnl": round(estimated_cash_pnl, 2),
        "realized_pnl": round(realized_pnl, 2),
        "exposure": round(exposure, 2),
        "position_count": len(positions),
        "average_position_size": round(exposure / len(sizes), 2) if sizes else 0.0,
        "largest_position_size": round(max(sizes), 2) if sizes else 0.0,
    }
