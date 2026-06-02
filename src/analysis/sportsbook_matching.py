from __future__ import annotations

from datetime import datetime
from typing import Any

from src.analysis.sports_parser import parse_market_record


def match_sportsbook_lines(polymarket_markets: list[dict[str, Any]], sportsbook_lines: list[dict[str, Any]], max_candidates: int = 20) -> list[dict[str, Any]]:
    candidates = [item["candidate"] for item in sportsbook_match_diagnostics(polymarket_markets, sportsbook_lines) if item["accepted"] and item.get("candidate")]
    candidates.sort(key=lambda item: item["confidence_score"], reverse=True)
    return candidates[:max_candidates]


def sportsbook_match_diagnostics(polymarket_markets: list[dict[str, Any]], sportsbook_lines: list[dict[str, Any]], source_venue: str = "polymarket") -> list[dict[str, Any]]:
    diagnostics: list[dict[str, Any]] = []
    parsed_pm = [(market, parse_market_record(market, "polymarket")) for market in polymarket_markets]
    for pm_market, pm in parsed_pm:
        for line in sportsbook_lines:
            candidate, diagnostic = score_sportsbook_match_with_diagnostic(pm_market, pm, line, source_venue=source_venue)
            diagnostic["candidate"] = candidate
            diagnostics.append(diagnostic)
    return diagnostics


def score_sportsbook_match(pm_market: dict[str, Any], pm: Any, line: dict[str, Any]) -> dict[str, Any] | None:
    candidate, _diagnostic = score_sportsbook_match_with_diagnostic(pm_market, pm, line)
    return candidate


def score_sportsbook_match_with_diagnostic(pm_market: dict[str, Any], pm: Any, line: dict[str, Any], source_venue: str = "polymarket") -> tuple[dict[str, Any] | None, dict[str, Any]]:
    target_title = _sportsbook_title(line)
    base = _diagnostic_base(pm_market, pm, line, source_venue, target_title)

    def reject(reason: str, confidence: float = 0.0) -> tuple[None, dict[str, Any]]:
        item = dict(base)
        item.update({"accepted": False, "rejection_reason": reason, "confidence_score": round(confidence, 4)})
        return None, item

    league = line.get("league")
    if not pm.league or not pm.team:
        return reject("missing structured fields")
    if not league or pm.league != league:
        return reject("league mismatch")

    book_team = str(line.get("team") or "").lower()
    book_opponent = str(line.get("opponent") or "").lower()
    market_type = line.get("market_type")
    if not book_team:
        return reject("missing structured fields")

    if pm.market_type == "championship_winner":
        if market_type in {"game_winner", "spread", "total", "player_prop"}:
            if market_type == "spread":
                return reject("spread mismatch", 0.35)
            if market_type == "total":
                return reject("total mismatch", 0.35)
            return reject("market type mismatch", 0.35)
        if market_type in {"conference_winner", "division_winner", "season_award"}:
            return reject("market type mismatch", 0.35)
        if market_type != "championship_winner":
            return reject("market type mismatch", 0.35)
        if not _team_matches(pm.team.lower(), book_team):
            return reject("team mismatch")
        if pm.season_year and line.get("season_year") and int(pm.season_year) != int(line.get("season_year")):
            return reject("event date mismatch", 0.5)
    else:
        if not _team_matches(pm.team.lower(), book_team) and not _team_matches(pm.team.lower(), book_opponent):
            return reject("team mismatch")
        if pm.opponent and book_opponent and not (_team_matches(pm.opponent.lower(), book_opponent) or _team_matches(pm.opponent.lower(), book_team)):
            return reject("opponent mismatch")

    if pm.market_type and market_type and not _market_types_compatible(pm.market_type, str(market_type)):
        if market_type == "spread":
            return reject("spread mismatch", 0.35)
        if market_type == "total":
            return reject("total mismatch", 0.35)
        return reject("market type mismatch", 0.35)
    if market_type == "spread" and line.get("line") is None:
        return reject("spread mismatch", 0.45)
    if market_type == "total" and line.get("line") is None:
        return reject("total mismatch", 0.45)
    if pm.season_year and line.get("commence_time") and str(pm.season_year) not in str(line.get("commence_time")):
        return reject("event date mismatch", 0.5)

    score = 0.45
    reasons = [f"league match: {pm.league}", "team/opponent compatible"]
    if pm.market_type and market_type:
        score += 0.2
        reasons.append(f"market type compatible: {pm.market_type} vs {market_type}")
    if line.get("commence_time"):
        score += 0.1
        reasons.append("sportsbook line has commence time")
    if market_type == "championship_winner":
        score += 0.1
        reasons.append("sportsbook futures market")
    if line.get("implied_probability") is not None:
        score += 0.15
        reasons.append("sportsbook implied probability available")
    if score < 0.7:
        return reject("confidence below threshold", score)

    title = str(pm_market.get("title") or pm_market.get("question") or pm_market.get("slug") or "")
    candidate = {
        "polymarket_id": str(pm_market.get("conditionId") or pm_market.get("condition_id") or pm_market.get("slug") or title),
        "polymarket_title": title,
        "sportsbook": line.get("bookmaker_name"),
        "sportsbook_event_id": line.get("event_id"),
        "sportsbook_team": line.get("team"),
        "sportsbook_opponent": line.get("opponent"),
        "sportsbook_market_title": line.get("market_title"),
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
        "structured_match": {source_venue: pm.to_dict(), "sportsbook": line},
    }
    item = dict(base)
    item.update({"accepted": True, "rejection_reason": None, "confidence_score": candidate["confidence_score"], "candidate": candidate})
    return candidate, item


def _diagnostic_base(pm_market: dict[str, Any], pm: Any, line: dict[str, Any], source_venue: str, target_title: str) -> dict[str, Any]:
    source_title = str(pm_market.get("title") or pm_market.get("question") or pm_market.get("slug") or pm_market.get("ticker") or "")
    return {
        "source_venue": source_venue,
        "target_venue": "sportsbook",
        "source_market_title": source_title,
        "target_market_title": target_title,
        "source_market_type": pm.market_type,
        "target_market_type": line.get("market_type"),
        "parsed_teams": {"source_team": pm.team, "target_team": line.get("team")},
        "parsed_opponents": {"source_opponent": pm.opponent, "target_opponent": line.get("opponent")},
        "parsed_league": {"source_league": pm.league, "target_league": line.get("league"), "raw_sport_key": line.get("raw_sport_key") or line.get("sport_key"), "raw_sport_title": line.get("raw_sport_title")},
        "source_league": pm.league,
        "target_league": line.get("league"),
        "raw_sport_key": line.get("raw_sport_key") or line.get("sport_key"),
        "raw_sport_title": line.get("raw_sport_title"),
        "parsed_event_date": {"source_season_year": pm.season_year, "target_commence_time": line.get("commence_time"), "target_season_year": line.get("season_year")},
        "parsed_fields": {source_venue: pm.to_dict(), "sportsbook": line},
    }


def _sportsbook_title(line: dict[str, Any]) -> str:
    if line.get("market_title") and line.get("team") and line.get("market_type") == "championship_winner":
        return f"{line.get('market_title')} - {line.get('team')}"
    teams = [str(value) for value in (line.get("team"), line.get("opponent")) if value]
    return " vs ".join(teams) or str(line.get("event_id") or "sportsbook line")


def _team_matches(left: str, right: str) -> bool:
    if not left or not right:
        return False
    left_parts = set(left.split())
    right_parts = set(right.split())
    return left in right or right in left or bool(left_parts & right_parts)


def _market_types_compatible(pm_type: str, sportsbook_type: str) -> bool:
    if pm_type == sportsbook_type:
        return True
    return pm_type == "championship_winner" and sportsbook_type == "championship_winner"
