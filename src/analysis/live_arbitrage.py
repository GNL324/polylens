from __future__ import annotations

from collections import Counter
from typing import Any

from src.analysis.arb_pricing import extract_kalshi_pricing, normalize_price, price_candidate
from src.analysis.cross_market import compare_wallet_markets_to_kalshi
from src.analysis.live_market_discovery import normalize_kalshi_live_markets, normalize_polymarket_live_markets, normalize_sportsbook_live_lines
from src.analysis.opportunity_scoring import filter_scored_candidates, score_candidates
from src.analysis.sportsbook_matching import match_sportsbook_lines


def scan_live_arbitrage(
    polymarket_markets: list[dict[str, Any]],
    kalshi_markets: list[dict[str, Any]],
    sportsbook_lines: list[dict[str, Any]] | None = None,
    sportsbook_skipped_reason: str | None = None,
    venue_errors: dict[str, str] | None = None,
    min_edge: float | None = None,
    min_score: float | None = None,
    max_close_hours: float | None = None,
    include_low_confidence: bool = False,
) -> dict[str, Any]:
    sportsbook_lines = sportsbook_lines or []
    venue_errors = venue_errors or {}
    pm_live = normalize_polymarket_live_markets(polymarket_markets)
    kalshi_live = normalize_kalshi_live_markets(kalshi_markets)
    sportsbook_live = normalize_sportsbook_live_lines(sportsbook_lines)
    skipped = Counter(venue_errors.values())
    if sportsbook_skipped_reason:
        skipped[sportsbook_skipped_reason] += 1

    pm_kalshi_matches = compare_wallet_markets_to_kalshi(polymarket_markets, kalshi_markets, max_candidates=50) if polymarket_markets and kalshi_markets else []
    pm_sportsbook_matches = match_sportsbook_lines(polymarket_markets, sportsbook_lines, max_candidates=50) if polymarket_markets and sportsbook_lines else []
    kalshi_sportsbook_matches = _match_kalshi_to_sportsbook(kalshi_markets, sportsbook_lines) if kalshi_markets and sportsbook_lines else []

    candidates: list[dict[str, Any]] = []
    pm_trade_map = [_synthetic_polymarket_trade(market) for market in polymarket_markets]
    kalshi_by_ticker = {str(market.get("ticker") or market.get("market_ticker")): market for market in kalshi_markets}
    for match in pm_kalshi_matches:
        kalshi_market = kalshi_by_ticker.get(str(match.get("kalshi_ticker")), {})
        candidate = price_candidate(match, pm_trade_map, kalshi_market)
        candidate["venue_pair"] = "polymarket_kalshi"
        candidates.append(candidate)

    for match in pm_sportsbook_matches:
        candidate = _price_polymarket_sportsbook(match, pm_trade_map)
        candidate["venue_pair"] = "polymarket_sportsbook"
        candidates.append(candidate)

    for match in kalshi_sportsbook_matches:
        candidate = _price_kalshi_sportsbook(match)
        candidate["venue_pair"] = "kalshi_sportsbook"
        candidates.append(candidate)

    if not pm_kalshi_matches and polymarket_markets and kalshi_markets:
        skipped["no conservative Polymarket/Kalshi live matches"] += 1
    if not pm_sportsbook_matches and polymarket_markets and sportsbook_lines:
        skipped["no unambiguous Polymarket/sportsbook live matches"] += 1
    if not kalshi_sportsbook_matches and kalshi_markets and sportsbook_lines:
        skipped["no unambiguous Kalshi/sportsbook live matches"] += 1
    if not polymarket_markets:
        skipped["no Polymarket live markets available"] += 1
    if not kalshi_markets:
        skipped["no Kalshi live markets available"] += 1

    scored_candidates = score_candidates(candidates)
    filtered_candidates, filter_reasons = filter_scored_candidates(
        scored_candidates,
        min_edge=min_edge,
        min_score=min_score,
        max_close_hours=max_close_hours,
        include_low_confidence=include_low_confidence,
    )
    return {
        "markets_scanned_by_venue": {"polymarket": len(pm_live), "kalshi": len(kalshi_live), "sportsbook": len(sportsbook_live)},
        "matches_found_by_venue_pair": {
            "polymarket_kalshi": len(pm_kalshi_matches),
            "polymarket_sportsbook": len(pm_sportsbook_matches),
            "kalshi_sportsbook": len(kalshi_sportsbook_matches),
        },
        "arbitrage_candidates_found": len(filtered_candidates),
        "candidates_before_filtering": len(scored_candidates),
        "candidates_after_filtering": len(filtered_candidates),
        "filter_reasons": filter_reasons,
        "top_candidates": filtered_candidates[:25],
        "top_scored_candidates": filtered_candidates[:25],
        "skipped_rejected_reason_counts": dict(skipped),
    }


def _synthetic_polymarket_trade(market: dict[str, Any]) -> dict[str, Any]:
    title = str(market.get("title") or market.get("question") or market.get("slug") or "")
    market_id = str(market.get("conditionId") or market.get("condition_id") or market.get("slug") or title)
    return {
        "conditionId": market_id,
        "title": title,
        "question": title,
        "slug": market.get("slug"),
        "price": _polymarket_yes_price(market),
        "outcome": "Yes",
        "outcomeIndex": 0,
        "size": 1,
        "timestamp": _numeric_timestamp(market.get("updatedAt") or market.get("createdAt")),
        "updatedAt": market.get("updatedAt"),
    }


def _polymarket_yes_price(market: dict[str, Any]) -> float | None:
    value = market.get("outcome_prices") or market.get("outcomePrices")
    if isinstance(value, str):
        import json
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            value = [part.strip() for part in value.split(",")]
    if isinstance(value, list) and value:
        return normalize_price(value[0])
    return normalize_price(market.get("price") or market.get("bestAsk") or market.get("lastTradePrice"))


def _price_polymarket_sportsbook(candidate: dict[str, Any], trades: list[dict[str, Any]]) -> dict[str, Any]:
    from src.analysis.arb_pricing import price_sportsbook_candidate

    return price_sportsbook_candidate(candidate, trades)


def _match_kalshi_to_sportsbook(kalshi_markets: list[dict[str, Any]], sportsbook_lines: list[dict[str, Any]]) -> list[dict[str, Any]]:
    pseudo_pm = []
    lookup: dict[str, dict[str, Any]] = {}
    for market in kalshi_markets:
        title = str(market.get("title") or market.get("subtitle") or market.get("ticker") or "")
        pseudo = {
            "conditionId": str(market.get("ticker") or title),
            "title": title,
            "question": title,
            "slug": market.get("ticker"),
            "outcome": "Yes",
        }
        lookup[pseudo["conditionId"]] = market
        pseudo_pm.append(pseudo)
    matches = match_sportsbook_lines(pseudo_pm, sportsbook_lines, max_candidates=50)
    for match in matches:
        kalshi_market = lookup.get(str(match.get("polymarket_id")), {})
        match["kalshi_ticker"] = match.pop("polymarket_id", None)
        match["kalshi_title"] = match.pop("polymarket_title", None)
        match["kalshi_market"] = kalshi_market
    return matches


def _price_kalshi_sportsbook(candidate: dict[str, Any]) -> dict[str, Any]:
    kalshi = extract_kalshi_pricing(candidate.get("kalshi_market") or {})
    yes_price = kalshi.get("yes_ask") or kalshi.get("kalshi_implied_yes_price")
    sportsbook_probability = candidate.get("sportsbook_implied_probability")
    result = dict(candidate)
    result.pop("kalshi_market", None)
    result.update({
        "kalshi_implied_yes_price": yes_price,
        "kalshi_liquidity": kalshi.get("liquidity"),
        "sportsbook_implied_probability": sportsbook_probability,
        "spread": _spread(yes_price, sportsbook_probability),
        "estimated_edge": None,
        "theoretical_sportsbook_arbitrage_exists": False,
        "pricing_status": "insufficient pricing data",
        "pricing_reason": "insufficient pricing data",
    })
    if yes_price is None or sportsbook_probability is None:
        return result
    edge = round(float(sportsbook_probability) - float(yes_price), 4)
    result["estimated_edge"] = edge
    result["pricing_status"] = "priced"
    if edge > 0:
        result["theoretical_sportsbook_arbitrage_exists"] = True
        result["pricing_reason"] = "Kalshi implied YES ask is below sportsbook implied probability before fees/slippage"
    else:
        result["pricing_reason"] = "no theoretical sportsbook edge at indicative prices"
    return result


def _spread(left: float | None, right: float | None) -> float | None:
    if left is None or right is None:
        return None
    return round(abs(float(left) - float(right)), 4)


def _numeric_timestamp(value: Any) -> float:
    if value is None or value == "":
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            from datetime import datetime

            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            try:
                return float(value)
            except ValueError:
                return 0.0
    return 0.0
