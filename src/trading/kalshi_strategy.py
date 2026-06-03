from __future__ import annotations

from typing import Any

from src.trading.risk import normalize_price


def market_signal(market: dict[str, Any], max_price: float = 0.5) -> dict[str, Any] | None:
    pricing = market.get("pricing") or {}
    raw_yes_ask = pricing.get("yes_ask") or pricing.get("best_ask") or market.get("yes_ask")
    ticker = market.get("ticker") or (market.get("market_identity") or {}).get("ticker")
    try:
        yes_ask = normalize_price(raw_yes_ask) if raw_yes_ask is not None else None
    except ValueError:
        yes_ask = None
    if ticker and yes_ask is not None and yes_ask <= max_price:
        return {"ticker": ticker, "side": "yes", "price": yes_ask, "count": 1, "reason": "yes ask below strategy threshold"}
    return None


def scan_markets_for_signals(markets: list[dict[str, Any]], max_price: float = 0.5, limit: int = 25) -> list[dict[str, Any]]:
    signals = []
    for market in markets:
        signal = market_signal(market, max_price=max_price)
        if signal:
            signals.append(signal)
        if len(signals) >= limit:
            break
    return signals
