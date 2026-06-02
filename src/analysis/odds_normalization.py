from __future__ import annotations

from typing import Any

SPORT_LEAGUE_MAP = {
    "basketball_nba": "NBA",
    "baseball_mlb": "MLB",
    "icehockey_nhl": "NHL",
    "americanfootball_nfl": "NFL",
}


def american_to_implied_probability(odds: int | float | str | None) -> float | None:
    if odds is None or odds == "":
        return None
    try:
        value = float(odds)
    except (TypeError, ValueError):
        return None
    if value > 0:
        return round(100 / (value + 100), 4)
    if value < 0:
        return round(abs(value) / (abs(value) + 100), 4)
    return None


def decimal_to_implied_probability(odds: int | float | str | None) -> float | None:
    if odds is None or odds == "":
        return None
    try:
        value = float(odds)
    except (TypeError, ValueError):
        return None
    if value <= 1:
        return None
    return round(1 / value, 4)


def normalize_odds_events(events: list[dict[str, Any]], odds_format: str = "american") -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for event in events:
        rows.extend(normalize_event_odds(event, odds_format=odds_format))
    return rows


def normalize_event_odds(event: dict[str, Any], odds_format: str = "american") -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    sport_key = str(event.get("sport_key") or "")
    league = SPORT_LEAGUE_MAP.get(sport_key, str(event.get("sport_title") or sport_key).upper() or None)
    teams = [team for team in (event.get("home_team"), event.get("away_team")) if team]
    for bookmaker in event.get("bookmakers", []) or []:
        for market in bookmaker.get("markets", []) or []:
            market_key = market.get("key")
            for outcome in market.get("outcomes", []) or []:
                team = outcome.get("name")
                opponent = _opponent(team, teams)
                price = outcome.get("price")
                implied = american_to_implied_probability(price) if odds_format == "american" else decimal_to_implied_probability(price)
                rows.append({
                    "event_id": event.get("id"),
                    "sport_key": sport_key,
                    "league": league,
                    "team": team,
                    "opponent": opponent,
                    "home_team": event.get("home_team"),
                    "away_team": event.get("away_team"),
                    "bookmaker_name": bookmaker.get("title") or bookmaker.get("key"),
                    "bookmaker_key": bookmaker.get("key"),
                    "market_type": _normalize_market_type(str(market_key or "")),
                    "line": outcome.get("point"),
                    "odds": price,
                    "odds_format": odds_format,
                    "implied_probability": implied,
                    "commence_time": event.get("commence_time"),
                    "last_update": market.get("last_update") or bookmaker.get("last_update"),
                    "raw": {"event": event, "bookmaker": bookmaker, "market": market, "outcome": outcome},
                })
    return rows


def _opponent(team: str | None, teams: list[str]) -> str | None:
    if not team:
        return None
    for candidate in teams:
        if candidate != team:
            return candidate
    return None


def _normalize_market_type(value: str) -> str:
    mapping = {"h2h": "game_winner", "spreads": "spread", "totals": "total", "outrights": "championship_winner"}
    return mapping.get(value, value)
