from __future__ import annotations

from collections import defaultdict
from typing import Any


def normalize_price(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    if numeric > 1 and numeric <= 100:
        numeric = numeric / 100
    if numeric < 0 or numeric > 1:
        return None
    return round(numeric, 4)


def numeric_amount(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    if numeric < 0:
        return None
    return round(numeric, 4)


def polymarket_implied_yes_price(trade: dict[str, Any]) -> float | None:
    price = normalize_price(trade.get("price"))
    if price is None:
        return None
    outcome = str(trade.get("outcome") or "").strip().lower()
    outcome_index = trade.get("outcomeIndex")
    yes_like = {"yes", "up", "above", "higher", "over", "true"}
    no_like = {"no", "down", "below", "lower", "under", "false"}
    if outcome in yes_like or outcome_index == 0:
        return price
    if outcome in no_like or outcome_index == 1:
        return round(1 - price, 4)
    return price


def summarize_polymarket_market_price(trades: list[dict[str, Any]], polymarket_id: str) -> dict[str, Any]:
    matching = [trade for trade in trades if _trade_market_id(trade) == polymarket_id]
    if not matching:
        return {"implied_yes_price": None, "source": "missing", "liquidity_warning": "insufficient pricing data: no matching Polymarket trade"}
    priced = [(trade, polymarket_implied_yes_price(trade)) for trade in matching]
    priced = [(trade, price) for trade, price in priced if price is not None]
    if not priced:
        return {"implied_yes_price": None, "source": "missing", "liquidity_warning": "insufficient pricing data: Polymarket trade price missing"}
    latest_trade, latest_price = max(priced, key=lambda item: float(item[0].get("timestamp") or 0))
    total_size = sum(float(trade.get("size") or 0) for trade, _price in priced)
    weighted = sum((float(trade.get("size") or 0) * price) for trade, price in priced) / total_size if total_size else latest_price
    return {
        "implied_yes_price": round(latest_price, 4),
        "volume_weighted_yes_price": round(weighted, 4),
        "source": "wallet_trade_latest",
        "trade_count": len(priced),
        "latest_trade_timestamp": latest_trade.get("timestamp"),
        "liquidity_warning": "Polymarket price is inferred from wallet trades, not a live order book.",
    }


def extract_kalshi_pricing(kalshi_market: dict[str, Any]) -> dict[str, Any]:
    pricing = kalshi_market.get("pricing") if isinstance(kalshi_market.get("pricing"), dict) else {}
    yes_bid = normalize_price(pricing.get("yes_bid") or kalshi_market.get("yes_bid_dollars") or kalshi_market.get("yes_bid"))
    yes_ask = normalize_price(pricing.get("yes_ask") or kalshi_market.get("yes_ask_dollars") or kalshi_market.get("yes_ask"))
    no_bid = normalize_price(pricing.get("no_bid") or kalshi_market.get("no_bid_dollars") or kalshi_market.get("no_bid"))
    no_ask = normalize_price(pricing.get("no_ask") or kalshi_market.get("no_ask_dollars") or kalshi_market.get("no_ask"))
    last_price = normalize_price(pricing.get("last_price") or kalshi_market.get("last_price_dollars") or kalshi_market.get("last_price"))
    liquidity = numeric_amount(pricing.get("liquidity") or kalshi_market.get("liquidity_dollars") or kalshi_market.get("liquidity"))
    price_data_missing = yes_ask is None or no_ask is None
    warnings = []
    if price_data_missing:
        warnings.append("insufficient pricing data: Kalshi yes/no ask prices missing")
    if liquidity is None or liquidity <= 0:
        warnings.append("liquidity warning: Kalshi liquidity is missing or zero")
    return {
        "kalshi_implied_yes_price": yes_ask if yes_ask is not None else last_price,
        "kalshi_implied_no_price": no_ask,
        "yes_bid": yes_bid,
        "yes_ask": yes_ask,
        "no_bid": no_bid,
        "no_ask": no_ask,
        "last_price": last_price,
        "liquidity": liquidity,
        "price_data_missing": price_data_missing,
        "liquidity_warning": "; ".join(warnings) if warnings else None,
    }


def enrich_candidates_with_pricing(candidates: list[dict[str, Any]], trades: list[dict[str, Any]], kalshi_markets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    kalshi_by_ticker = {str(market.get("ticker") or market.get("market_ticker")): market for market in kalshi_markets}
    enriched = []
    for candidate in candidates:
        kalshi_market = kalshi_by_ticker.get(str(candidate.get("kalshi_ticker")), {})
        pricing = price_candidate(candidate, trades, kalshi_market)
        enriched.append(pricing)
    enriched.sort(key=lambda item: (item.get("theoretical_yes_no_arbitrage_exists") is True, item.get("estimated_edge") or -1), reverse=True)
    return enriched


def price_candidate(candidate: dict[str, Any], trades: list[dict[str, Any]], kalshi_market: dict[str, Any]) -> dict[str, Any]:
    pm = summarize_polymarket_market_price(trades, str(candidate.get("polymarket_id")))
    kalshi = extract_kalshi_pricing(kalshi_market)
    pm_yes = pm.get("implied_yes_price")
    pm_no = round(1 - pm_yes, 4) if pm_yes is not None else None
    kalshi_yes = kalshi.get("yes_ask")
    kalshi_no = kalshi.get("no_ask")
    warnings = [warning for warning in [pm.get("liquidity_warning"), kalshi.get("liquidity_warning")] if warning]
    price_data_missing = pm_yes is None or pm_no is None or kalshi_yes is None or kalshi_no is None

    result = dict(candidate)
    result.update({
        "polymarket_implied_yes_price": pm_yes,
        "polymarket_implied_no_price": pm_no,
        "kalshi_implied_yes_price": kalshi.get("kalshi_implied_yes_price"),
        "kalshi_implied_no_price": kalshi.get("kalshi_implied_no_price"),
        "kalshi_best_bid": kalshi.get("yes_bid"),
        "kalshi_best_ask": kalshi.get("yes_ask"),
        "kalshi_last_price": kalshi.get("last_price"),
        "spread": _spread(pm_yes, kalshi.get("kalshi_implied_yes_price")),
        "estimated_edge": None,
        "theoretical_yes_no_arbitrage_exists": False,
        "arbitrage_direction": None,
        "price_data_missing": price_data_missing,
        "liquidity_warning": "; ".join(warnings) if warnings else None,
        "pricing_status": "insufficient pricing data" if price_data_missing else "priced",
    })
    if price_data_missing:
        result["pricing_reason"] = "insufficient pricing data"
        return result

    buy_pm_yes_kalshi_no = round(1 - (pm_yes + kalshi_no), 4)
    buy_kalshi_yes_pm_no = round(1 - (kalshi_yes + pm_no), 4)
    edge = max(buy_pm_yes_kalshi_no, buy_kalshi_yes_pm_no)
    result["estimated_edge"] = round(edge, 4)
    if edge > 0:
        result["theoretical_yes_no_arbitrage_exists"] = True
        result["arbitrage_direction"] = "buy_polymarket_yes_buy_kalshi_no" if buy_pm_yes_kalshi_no >= buy_kalshi_yes_pm_no else "buy_kalshi_yes_buy_polymarket_no"
        result["pricing_reason"] = "theoretical yes/no coverage cost is below $1 before fees/slippage"
    else:
        result["pricing_reason"] = "no theoretical yes/no arbitrage at available indicative prices"
    return result


def _trade_market_id(trade: dict[str, Any]) -> str:
    return str(trade.get("conditionId") or trade.get("slug") or trade.get("title") or "unknown")


def _spread(left: float | None, right: float | None) -> float | None:
    if left is None or right is None:
        return None
    return round(abs(left - right), 4)
