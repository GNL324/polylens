from __future__ import annotations

import json
from typing import Any

from src.analysis.arb_pricing import normalize_price, numeric_amount
from src.analysis.crypto_parser import parse_market_record as parse_crypto_market
from src.analysis.markets import infer_category
from src.analysis.sports_parser import parse_market_record as parse_sports_market


def normalize_polymarket_live_markets(markets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [_normalize_polymarket_market(market) for market in markets]


def normalize_kalshi_live_markets(markets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [_normalize_kalshi_market(market) for market in markets]


def normalize_sportsbook_live_lines(lines: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [_normalize_sportsbook_line(line) for line in lines]


def _normalize_polymarket_market(market: dict[str, Any]) -> dict[str, Any]:
    sports = parse_sports_market(market, "polymarket")
    crypto = parse_crypto_market(market, "polymarket")
    title = str(market.get("title") or market.get("question") or market.get("slug") or "")
    category = market.get("category") or ("Crypto" if crypto.asset_symbol else None) or ("Sports" if sports.league else None) or infer_category({"title": title})
    return {
        "venue": "polymarket",
        "market_id": str(market.get("condition_id") or market.get("conditionId") or market.get("market_id") or market.get("id") or market.get("slug") or title),
        "title": title,
        "category": category,
        "league": sports.league,
        "asset_or_team": crypto.asset_symbol or sports.team,
        "opponent": sports.opponent,
        "market_type": crypto.market_type or sports.market_type,
        "price_data": {
            "yes_price": _polymarket_yes_price(market),
            "outcome_prices": _coerce_sequence(market.get("outcome_prices") or market.get("outcomePrices")),
        },
        "close_or_commence_time": market.get("end_date") or market.get("endDate") or market.get("closeTime"),
        "liquidity": numeric_amount(market.get("liquidity") or market.get("liquidityNum")),
        "raw": market,
        "parsed": {"sports": sports.to_dict(), "crypto": crypto.to_dict()},
    }


def _normalize_kalshi_market(market: dict[str, Any]) -> dict[str, Any]:
    sports = parse_sports_market(market, "kalshi")
    crypto = parse_crypto_market(market, "kalshi")
    pricing = market.get("pricing") if isinstance(market.get("pricing"), dict) else {}
    identity = market.get("market_identity") if isinstance(market.get("market_identity"), dict) else {}
    title = str(market.get("title") or identity.get("title") or market.get("ticker") or "")
    category = market.get("category") or identity.get("category") or ("Crypto" if crypto.asset_symbol else None) or ("Sports" if sports.league else None) or infer_category({"title": title})
    return {
        "venue": "kalshi",
        "market_id": str(market.get("ticker") or identity.get("ticker") or title),
        "title": title,
        "category": category,
        "league": sports.league,
        "asset_or_team": crypto.asset_symbol or sports.team,
        "opponent": sports.opponent,
        "market_type": crypto.market_type or sports.market_type,
        "price_data": {
            "yes_bid": normalize_price(pricing.get("yes_bid") or market.get("yes_bid_dollars") or market.get("yes_bid")),
            "yes_ask": normalize_price(pricing.get("yes_ask") or market.get("yes_ask_dollars") or market.get("yes_ask")),
            "no_bid": normalize_price(pricing.get("no_bid") or market.get("no_bid_dollars") or market.get("no_bid")),
            "no_ask": normalize_price(pricing.get("no_ask") or market.get("no_ask_dollars") or market.get("no_ask")),
            "last_price": normalize_price(pricing.get("last_price") or market.get("last_price_dollars") or market.get("last_price")),
        },
        "close_or_commence_time": market.get("close_time") or identity.get("close_time") or market.get("expiration_time") or identity.get("expiration_time"),
        "liquidity": numeric_amount(pricing.get("liquidity") or market.get("liquidity_dollars") or market.get("liquidity")),
        "raw": market,
        "parsed": {"sports": sports.to_dict(), "crypto": crypto.to_dict()},
    }


def _normalize_sportsbook_line(line: dict[str, Any]) -> dict[str, Any]:
    return {
        "venue": "sportsbook",
        "market_id": str(line.get("event_id") or ""),
        "title": " vs ".join(value for value in [str(line.get("team") or ""), str(line.get("opponent") or "")] if value),
        "category": "Sports",
        "league": line.get("league"),
        "asset_or_team": line.get("team"),
        "opponent": line.get("opponent"),
        "market_type": line.get("market_type"),
        "price_data": {
            "bookmaker": line.get("bookmaker_name"),
            "odds": line.get("odds"),
            "implied_probability": line.get("implied_probability"),
            "line": line.get("line"),
        },
        "close_or_commence_time": line.get("commence_time"),
        "liquidity": None,
        "raw": line,
        "parsed": {"sportsbook": line},
    }


def _polymarket_yes_price(market: dict[str, Any]) -> float | None:
    prices = _coerce_sequence(market.get("outcome_prices") or market.get("outcomePrices"))
    outcomes = [str(value).strip().lower() for value in _coerce_sequence(market.get("outcomes"))]
    if not prices:
        return normalize_price(market.get("price") or market.get("bestAsk") or market.get("lastTradePrice"))
    if outcomes and "yes" in outcomes:
        index = outcomes.index("yes")
        if index < len(prices):
            return normalize_price(prices[index])
    return normalize_price(prices[0])


def _coerce_sequence(value: Any) -> list[Any]:
    if value is None or value == "":
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return [part.strip() for part in value.split(",") if part.strip()]
        return parsed if isinstance(parsed, list) else []
    return []
