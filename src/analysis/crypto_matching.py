from __future__ import annotations

from datetime import date
from typing import Any

from src.analysis.crypto_parser import ParsedCryptoMarket, parse_market_record
from src.analysis.market_normalization import normalize_text

PRICE_TOLERANCE = 0.01
EXPIRY_WINDOW_DAYS = 2


def structured_crypto_candidates(polymarket_markets: list[dict[str, Any]], kalshi_markets: list[dict[str, Any]], max_candidates: int = 10) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    parsed_pm = [(market, parse_market_record(market, "polymarket")) for market in polymarket_markets]
    parsed_kalshi = [(market, parse_market_record(market, "kalshi")) for market in kalshi_markets]
    for pm_market, pm_parsed in parsed_pm:
        if not pm_parsed.asset_symbol:
            continue
        for kalshi_market, kalshi_parsed in parsed_kalshi:
            candidate = score_structured_crypto_pair(pm_market, pm_parsed, kalshi_market, kalshi_parsed)
            if candidate:
                candidates.append(candidate)
    candidates.sort(key=lambda item: (item["similarity_score"], len(item["shared_keywords_entities"])), reverse=True)
    return candidates[:max_candidates]


def score_structured_crypto_pair(pm_market: dict[str, Any], pm: ParsedCryptoMarket, kalshi_market: dict[str, Any], kalshi: ParsedCryptoMarket) -> dict[str, Any] | None:
    if not pm.asset_symbol or not kalshi.asset_symbol or pm.asset_symbol != kalshi.asset_symbol:
        return None
    if not _directions_compatible(pm.direction, kalshi.direction):
        return None
    if not _targets_compatible(pm, kalshi):
        return None
    if not _expiry_compatible(pm.expiry_date, kalshi.expiry_date):
        return None
    if pm.market_type and kalshi.market_type and not _market_types_compatible(pm.market_type, kalshi.market_type):
        return None

    score = 0.35
    reasons = [f"asset match: {pm.asset_symbol}"]
    shared_terms = {pm.asset_symbol.lower(), (pm.asset_name or pm.asset_symbol).lower()}
    if pm.direction and kalshi.direction:
        score += 0.15
        shared_terms.add(pm.direction if pm.direction == kalshi.direction else f"{pm.direction}/{kalshi.direction}")
        reasons.append(f"direction compatible: {pm.direction} vs {kalshi.direction}")
    if _targets_compatible(pm, kalshi):
        score += 0.24
        shared_terms.update(_target_terms(pm))
        reasons.append("target price compatible")
    if pm.expiry_date and kalshi.expiry_date:
        score += 0.16
        shared_terms.add(pm.expiry_date)
        reasons.append(f"expiry window compatible: {pm.expiry_date} vs {kalshi.expiry_date}")
    if pm.market_type and kalshi.market_type and _market_types_compatible(pm.market_type, kalshi.market_type):
        score += 0.10
        shared_terms.add(pm.market_type)
        reasons.append(f"market type compatible: {pm.market_type} vs {kalshi.market_type}")
    if score < 0.72:
        return None

    score = min(score, 0.99)
    confidence = "high" if score >= 0.9 else "medium" if score >= 0.78 else "low"
    pm_title = str(pm_market.get("title") or pm_market.get("slug") or pm_market.get("conditionId") or "")
    kalshi_title = str(kalshi_market.get("title") or kalshi_market.get("ticker") or "")
    return {
        "normalized_title": normalize_text(pm_title),
        "polymarket_id": str(pm_market.get("conditionId") or pm_market.get("slug") or pm_title),
        "polymarket_title": pm_title,
        "kalshi_ticker": str(kalshi_market.get("ticker") or kalshi_market.get("market_ticker") or kalshi_title),
        "kalshi_title": kalshi_title,
        "shared_keywords_entities": sorted(shared_terms),
        "sport_league_category_guess": {"category": "Crypto", "league": None},
        "similarity_score": round(score, 4),
        "confidence_band": confidence,
        "reason": "; ".join(reasons),
        "structured_match": {"polymarket": pm.to_dict(), "kalshi": kalshi.to_dict()},
    }


def _directions_compatible(left: str | None, right: str | None) -> bool:
    if not left or not right:
        return False
    return left == right or {left, right} == {"above", "touches"}


def _targets_compatible(left: ParsedCryptoMarket, right: ParsedCryptoMarket) -> bool:
    if left.direction == "between" or right.direction == "between":
        if None in (left.lower_bound, left.upper_bound, right.lower_bound, right.upper_bound):
            return False
        return _close(left.lower_bound, right.lower_bound) and _close(left.upper_bound, right.upper_bound)
    if left.target_price is None or right.target_price is None:
        return False
    return _close(left.target_price, right.target_price)


def _close(left: float | None, right: float | None) -> bool:
    if left is None or right is None or left == 0:
        return False
    return abs(left - right) / left <= PRICE_TOLERANCE


def _expiry_compatible(left: str | None, right: str | None) -> bool:
    if not left or not right:
        return False
    try:
        left_date = date.fromisoformat(left)
        right_date = date.fromisoformat(right)
    except ValueError:
        return False
    return abs((left_date - right_date).days) <= EXPIRY_WINDOW_DAYS


def _market_types_compatible(left: str, right: str) -> bool:
    if left == right:
        return True
    return {left, right} <= {"price target", "daily close", "weekly close", "monthly close"}


def _target_terms(parsed: ParsedCryptoMarket) -> set[str]:
    if parsed.direction == "between":
        return {str(int(value)) for value in (parsed.lower_bound, parsed.upper_bound) if value is not None and value.is_integer()}
    if parsed.target_price is None:
        return set()
    return {str(int(parsed.target_price)) if parsed.target_price.is_integer() else str(parsed.target_price)}
