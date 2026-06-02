from __future__ import annotations

from typing import Any

FUTURES_MARKET_TYPES = {
    "outrights": "championship_winner",
    "championship_winner": "championship_winner",
    "conference_winner": "conference_winner",
    "division_winner": "division_winner",
    "season_awards": "season_award",
    "awards": "season_award",
}

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


def normalize_futures_events(events: list[dict[str, Any]], odds_format: str = "american") -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for event in events:
        rows.extend(normalize_event_futures(event, odds_format=odds_format))
    return rows


def normalize_event_futures(event: dict[str, Any], odds_format: str = "american") -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    sport_key = str(event.get("sport_key") or "")
    league = SPORT_LEAGUE_MAP.get(sport_key, str(event.get("sport_title") or sport_key).upper() or None)
    event_title = str(event.get("title") or event.get("description") or event.get("sport_title") or "")
    for bookmaker in event.get("bookmakers", []) or []:
        for market in bookmaker.get("markets", []) or []:
            market_key = str(market.get("key") or "")
            market_type = _normalize_futures_market_type(market_key, event_title, market)
            for outcome in market.get("outcomes", []) or []:
                price = outcome.get("price")
                implied = american_to_implied_probability(price) if odds_format == "american" else decimal_to_implied_probability(price)
                rows.append({
                    "event_id": event.get("id") or f"{sport_key}:{market_key}",
                    "sport_key": sport_key,
                    "league": league,
                    "team": outcome.get("name"),
                    "opponent": None,
                    "home_team": None,
                    "away_team": None,
                    "bookmaker_name": bookmaker.get("title") or bookmaker.get("key"),
                    "bookmaker_key": bookmaker.get("key"),
                    "market_type": market_type,
                    "futures_market_type": market_type,
                    "line": outcome.get("point"),
                    "odds": price,
                    "odds_format": odds_format,
                    "implied_probability": implied,
                    "commence_time": event.get("commence_time"),
                    "last_update": market.get("last_update") or bookmaker.get("last_update"),
                    "season_year": _infer_futures_season(event, market),
                    "market_title": _futures_market_title(event, market, market_type),
                    "raw": {"event": event, "bookmaker": bookmaker, "market": market, "outcome": outcome},
                })
    return rows


def _normalize_futures_market_type(market_key: str, event_title: str, market: dict[str, Any]) -> str:
    key = market_key.lower()
    title = f" {str(event_title or '')} {str(market.get('title') or market.get('description') or '')} ".lower()
    if "conference" in key or "conference" in title:
        return "conference_winner"
    if "division" in key or "division" in title:
        return "division_winner"
    if "award" in key or "mvp" in title or "rookie" in title:
        return "season_award"
    if key in FUTURES_MARKET_TYPES:
        return FUTURES_MARKET_TYPES[key]
    return "championship_winner" if key == "outrights" else _normalize_market_type(key)


def _futures_market_title(event: dict[str, Any], market: dict[str, Any], market_type: str) -> str:
    title = event.get("title") or event.get("description") or market.get("title")
    if title:
        return str(title)
    league = SPORT_LEAGUE_MAP.get(str(event.get("sport_key") or ""), str(event.get("sport_title") or "League"))
    label = {
        "championship_winner": "Championship Winner",
        "conference_winner": "Conference Winner",
        "division_winner": "Division Winner",
        "season_award": "Season Award",
    }.get(market_type, "Futures")
    return f"{league} {label}"


def _infer_futures_season(event: dict[str, Any], market: dict[str, Any]) -> int | None:
    import re

    text = " ".join(str(value or "") for value in (event.get("title"), event.get("description"), event.get("commence_time"), market.get("last_update")))
    match = re.search(r"\b(20\d{2})\b", text)
    return int(match.group(1)) if match else None
