from __future__ import annotations

from datetime import datetime
from typing import Any

from src.analysis.sports_parser import parse_market_record


def match_sportsbook_lines(polymarket_markets: list[dict[str, Any]], sportsbook_lines: list[dict[str, Any]], max_candidates: int = 20) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    parsed_pm = [(market, parse_market_record(market, "polymarket")) for market in polymarket_markets]
    for pm_market, pm in parsed_pm:
        if not pm.league:
            continue
        for line in sportsbook_lines:
            candidate = score_sportsbook_match(pm_market, pm, line)
            if candidate:
                candidates.append(candidate)
    candidates.sort(key=lambda item: item["confidence_score"], reverse=True)
    return candidates[:max_candidates]


def score_sportsbook_match(pm_market: dict[str, Any], pm: Any, line: dict[str, Any]) -> dict[str, Any] | None:
    league = line.get("league")
    if not league or pm.league != league:
        return None
    if not pm.team:
        return None
    teams = {str(line.get("team") or "").lower(), str(line.get("opponent") or "").lower()}
    pm_teams = {value.lower() for value in (pm.team, pm.opponent) if value}
    if pm_teams and not all(any(_team_matches(pm_team, candidate) for candidate in teams) for pm_team in pm_teams):
        return None
    market_type = line.get("market_type")
    if pm.market_type and market_type and not _market_types_compatible(pm.market_type, str(market_type)):
        return None
    if market_type in {"spread", "total"} and line.get("line") is None:
        return None
    if pm.season_year and line.get("commence_time") and str(pm.season_year) not in str(line.get("commence_time")):
        return None
    score = 0.45
    reasons = [f"league match: {pm.league}", "team/opponent compatible"]
    if pm.market_type and market_type:
        score += 0.2
        reasons.append(f"market type compatible: {pm.market_type} vs {market_type}")
    if line.get("commence_time"):
        score += 0.1
        reasons.append("sportsbook line has commence time")
    if line.get("implied_probability") is not None:
        score += 0.15
        reasons.append("sportsbook implied probability available")
    if score < 0.7:
        return None
    title = str(pm_market.get("title") or pm_market.get("question") or pm_market.get("slug") or "")
    return {
        "polymarket_id": str(pm_market.get("conditionId") or pm_market.get("condition_id") or pm_market.get("slug") or title),
        "polymarket_title": title,
        "sportsbook": line.get("bookmaker_name"),
        "sportsbook_event_id": line.get("event_id"),
        "sportsbook_team": line.get("team"),
        "sportsbook_opponent": line.get("opponent"),
        "league": pm.league,
        "market_type": market_type,
        "line": line.get("line"),
        "commence_time": line.get("commence_time"),
        "last_update": line.get("last_update"),
        "sportsbook_implied_probability": line.get("implied_probability"),
        "odds": line.get("odds"),
        "confidence_score": round(min(score, 0.95), 4),
        "confidence_band": "high" if score >= 0.85 else "medium",
        "reason": "; ".join(reasons),
        "structured_match": {"polymarket": pm.to_dict(), "sportsbook": line},
    }


def _team_matches(left: str, right: str) -> bool:
    if not left or not right:
        return False
    left_parts = set(left.split())
    right_parts = set(right.split())
    return left in right or right in left or bool(left_parts & right_parts)


def _market_types_compatible(pm_type: str, sportsbook_type: str) -> bool:
    if pm_type == sportsbook_type:
        return True
    return pm_type in {"game_winner", "championship_winner"} and sportsbook_type in {"game_winner", "championship_winner"}
