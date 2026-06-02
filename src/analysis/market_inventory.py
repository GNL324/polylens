from __future__ import annotations

from collections import Counter
from typing import Any

from src.analysis.crypto_parser import parse_market_record as parse_crypto_market
from src.analysis.markets import infer_category
from src.analysis.sports_parser import parse_market_record as parse_sports_market

OPEN_STATUSES = {"open", "active", "live"}
CLOSED_STATUSES = {"closed", "settled", "resolved", "expired", "finalized", "archived"}


def summarize_market_inventory(polymarket_markets: list[dict[str, Any]], kalshi_markets: list[dict[str, Any]], match_summary: dict[str, Any] | None = None) -> dict[str, Any]:
    pm = _summarize_side(polymarket_markets, "polymarket")
    kalshi = _summarize_side(kalshi_markets, "kalshi")
    summary = {
        "polymarket": pm,
        "kalshi": kalshi,
        "polymarket_open_count": pm["status_counts"].get("open", 0),
        "polymarket_closed_count": pm["status_counts"].get("closed", 0),
        "kalshi_open_count": kalshi["status_counts"].get("open", 0),
        "kalshi_closed_count": kalshi["status_counts"].get("closed", 0),
        "unparsed_polymarket_count": pm["unparsed_count"],
        "unparsed_kalshi_count": kalshi["unparsed_count"],
    }
    summary["top_reasons_no_matches_available"] = _top_reasons(summary, match_summary or {})
    summary["likely_zero_candidate_reason"] = summary["top_reasons_no_matches_available"][0] if summary["top_reasons_no_matches_available"] else "matches available"
    return summary


def _summarize_side(markets: list[dict[str, Any]], source: str) -> dict[str, Any]:
    category_counts: Counter[str] = Counter()
    asset_counts: Counter[str] = Counter()
    league_counts: Counter[str] = Counter()
    status_counts: Counter[str] = Counter()
    crypto_types: Counter[str] = Counter()
    sports_types: Counter[str] = Counter()
    unparsed_examples: list[dict[str, Any]] = []
    unparsed_count = 0

    for market in markets:
        status_counts[_status_bucket(market, source)] += 1
        crypto = parse_crypto_market(market, source)
        sports = parse_sports_market(market, source)
        parsed = False
        if crypto.asset_symbol:
            parsed = True
            category_counts["Crypto"] += 1
            asset_counts[crypto.asset_symbol] += 1
            if crypto.market_type:
                crypto_types[crypto.market_type] += 1
        if sports.league:
            parsed = True
            category_counts["Sports"] += 1
            league_counts[sports.league] += 1
            if sports.market_type:
                sports_types[sports.market_type] += 1
        if not parsed:
            category = infer_category(_market_text_proxy(market))
            category_counts[category] += 1
            unparsed_count += 1
            if len(unparsed_examples) < 10:
                unparsed_examples.append(_example(market, source))
    return {
        "total_count": len(markets),
        "category_counts": dict(category_counts),
        "asset_counts": dict(asset_counts),
        "league_counts": dict(league_counts),
        "status_counts": dict(status_counts),
        "crypto_market_types_found": dict(crypto_types),
        "sports_market_types_found": dict(sports_types),
        "unparsed_count": unparsed_count,
        "unparsed_examples": unparsed_examples,
    }


def _status_bucket(market: dict[str, Any], source: str) -> str:
    value = market.get("status")
    if source == "kalshi" and isinstance(market.get("market_identity"), dict):
        value = value or market["market_identity"].get("status")
    resolved = market.get("resolved")
    if resolved is True:
        return "closed"
    if resolved is False and not value:
        return "open"
    normalized = str(value or "").lower()
    if normalized in OPEN_STATUSES or value is True:
        return "open"
    if normalized in CLOSED_STATUSES or value is False:
        return "closed"
    return "unknown"


def _market_text_proxy(market: dict[str, Any]) -> dict[str, Any]:
    return {
        "title": market.get("title") or market.get("question"),
        "slug": market.get("slug") or market.get("ticker"),
        "eventSlug": market.get("event_slug") or market.get("eventSlug") or market.get("event_ticker"),
        "category": market.get("category"),
    }


def _example(market: dict[str, Any], source: str) -> dict[str, Any]:
    return {
        "source": source,
        "title": market.get("title") or market.get("question"),
        "id": market.get("condition_id") or market.get("conditionId") or market.get("ticker") or market.get("market_id"),
        "status": _status_bucket(market, source),
    }


def _top_reasons(summary: dict[str, Any], match_summary: dict[str, Any]) -> list[str]:
    accepted = len(match_summary.get("accepted_matches", [])) if match_summary else 0
    if accepted:
        return ["matches available"]
    pm = summary["polymarket"]
    kalshi = summary["kalshi"]
    reasons: list[str] = []
    if pm["category_counts"].get("Crypto", 0) and not kalshi["category_counts"].get("Crypto", 0):
        reasons.append("no Kalshi crypto coverage in inspected inventory")
    if pm["category_counts"].get("Sports", 0) and not kalshi["category_counts"].get("Sports", 0):
        reasons.append("no Kalshi sports coverage in inspected inventory")
    missing_assets = sorted(set(pm["asset_counts"]) - set(kalshi["asset_counts"]))
    if missing_assets:
        reasons.append("Kalshi inventory missing wallet crypto assets: " + ", ".join(missing_assets[:5]))
    missing_leagues = sorted(set(pm["league_counts"]) - set(kalshi["league_counts"]))
    if missing_leagues:
        reasons.append("Kalshi inventory missing wallet sports leagues: " + ", ".join(missing_leagues[:5]))
    if summary["polymarket_closed_count"] > summary["polymarket_open_count"] and summary["polymarket_closed_count"] > 0:
        reasons.append("wallet markets appear mostly closed or expired")
    if summary["unparsed_polymarket_count"]:
        reasons.append("parser gaps on Polymarket markets")
    rejected = match_summary.get("top_rejected_candidate_reasons", []) if match_summary else []
    if rejected:
        reasons.append("structured candidates rejected: " + rejected[0]["reason"])
    if not reasons:
        reasons.append("no compatible Kalshi markets after structured and fallback matching")
    return reasons
