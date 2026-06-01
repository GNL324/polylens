from __future__ import annotations

from typing import Any


def money_value(trade: dict[str, Any]) -> float:
    if trade.get("usdcSize") is not None:
        return float(trade.get("usdcSize") or 0)
    size = float(trade.get("size") or 0)
    price = float(trade.get("price") or 0)
    return size * price


def summarize_volume(trades: list[dict[str, Any]]) -> dict[str, Any]:
    values = [money_value(trade) for trade in trades]
    total = sum(values)
    largest_index = max(range(len(values)), key=values.__getitem__) if values else None
    largest_trade = trades[largest_index] if largest_index is not None else None
    return {
        "trade_count": len(trades),
        "total_volume": round(total, 2),
        "average_trade_size": round(total / len(values), 2) if values else 0.0,
        "largest_trade": largest_trade,
        "largest_trade_size": round(values[largest_index], 2) if largest_index is not None else 0.0,
    }
