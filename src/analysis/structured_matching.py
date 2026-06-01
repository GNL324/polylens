from __future__ import annotations

from typing import Any

from src.analysis.market_normalization import normalize_text
from src.analysis.sports_parser import ParsedSportsMarket, parse_market_record


def structured_sports_candidates(polymarket_markets: list[dict[str, Any]], kalshi_markets: list[dict[str, Any]], max_candidates: int = 10) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    parsed_pm = [(market, parse_market_record(market, "polymarket")) for market in polymarket_markets]
    parsed_kalshi = [(market, parse_market_record(market, "kalshi")) for market in kalshi_markets]
    for pm_market, pm_parsed in parsed_pm:
        if not pm_parsed.league:
            continue
        for kalshi_market, kalshi_parsed in parsed_kalshi:
            candidate = score_structured_sports_pair(pm_market, pm_parsed, kalshi_market, kalshi_parsed)
            if candidate:
                candidates.append(candidate)
    candidates.sort(key=lambda item: (item["similarity_score"], len(item["shared_keywords_entities"])), reverse=True)
    return candidates[:max_candidates]


def score_structured_sports_pair(pm_market: dict[str, Any], pm: ParsedSportsMarket, kalshi_market: dict[str, Any], kalshi: ParsedSportsMarket) -> dict[str, Any] | None:
    if not pm.league or not kalshi.league:
        return None
    if pm.league != kalshi.league:
        return None
    if pm.season_year and kalshi.season_year and pm.season_year != kalshi.season_year:
        return None
    pm_teams = {value for value in (pm.team, pm.opponent) if value}
    kalshi_teams = {value for value in (kalshi.team, kalshi.opponent) if value}
    shared_teams = pm_teams & kalshi_teams
    if pm_teams and kalshi_teams and not shared_teams:
        return None
    if not shared_teams and not (pm.event_type and kalshi.event_type and pm.event_type == kalshi.event_type and pm.season_year and kalshi.season_year):
        return None

    score = 0.25
    reasons = [f"league match: {pm.league}"]
    shared_terms = {pm.league.lower()}
    if shared_teams:
        score += 0.38
        shared_terms.update(_terms_for_teams(shared_teams))
        reasons.append("team match: " + ", ".join(sorted(shared_teams)))
    if pm.season_year and kalshi.season_year and pm.season_year == kalshi.season_year:
        score += 0.16
        shared_terms.add(str(pm.season_year))
        reasons.append(f"season match: {pm.season_year}")
    if pm.event_type and kalshi.event_type and pm.event_type == kalshi.event_type:
        score += 0.11
        shared_terms.add(pm.event_type)
        reasons.append(f"event type match: {pm.event_type}")
    if pm.market_type and kalshi.market_type and pm.market_type == kalshi.market_type:
        score += 0.10
        shared_terms.add(pm.market_type)
        reasons.append(f"market type match: {pm.market_type}")
    if score < 0.62:
        return None

    score = min(score, 0.98)
    confidence = "high" if score >= 0.86 else "medium" if score >= 0.72 else "low"
    pm_title = str(pm_market.get("title") or pm_market.get("slug") or pm_market.get("conditionId") or "")
    kalshi_title = str(kalshi_market.get("title") or kalshi_market.get("ticker") or "")
    return {
        "normalized_title": normalize_text(pm_title),
        "polymarket_id": str(pm_market.get("conditionId") or pm_market.get("slug") or pm_title),
        "polymarket_title": pm_title,
        "kalshi_ticker": str(kalshi_market.get("ticker") or kalshi_market.get("market_ticker") or kalshi_title),
        "kalshi_title": kalshi_title,
        "shared_keywords_entities": sorted(shared_terms),
        "sport_league_category_guess": {"category": "Sports", "league": pm.league},
        "similarity_score": round(score, 4),
        "confidence_band": confidence,
        "reason": "; ".join(reasons),
        "structured_match": {"polymarket": pm.to_dict(), "kalshi": kalshi.to_dict()},
    }


def _terms_for_teams(teams: set[str]) -> set[str]:
    terms: set[str] = set()
    for team in teams:
        terms.update(team.split())
    return terms
