from __future__ import annotations

from typing import Any

from src.analysis.odds_normalization import american_to_implied_probability, decimal_to_implied_probability, normalize_sport_league

PLAYER_PROP_MARKETS = {
    "player_points",
    "player_rebounds",
    "player_assists",
    "player_threes",
    "player_blocks",
    "player_steals",
    "player_turnovers",
    "player_points_rebounds_assists",
    "player_points_rebounds",
    "player_points_assists",
    "player_rebounds_assists",
}


def normalize_player_props(events: list[dict[str, Any]], odds_format: str = "american") -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for event in events:
        rows.extend(normalize_event_player_props(event, odds_format=odds_format))
    return rows


def normalize_event_player_props(event: dict[str, Any], odds_format: str = "american") -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    sport_key = str(event.get("sport_key") or "")
    league = normalize_sport_league(sport_key, event.get("sport_title"))
    home_team = event.get("home_team")
    away_team = event.get("away_team")
    for bookmaker in event.get("bookmakers", []) or []:
        for market in bookmaker.get("markets", []) or []:
            market_key = str(market.get("key") or "")
            if market_key not in PLAYER_PROP_MARKETS:
                continue
            for outcome in market.get("outcomes", []) or []:
                side = _normalize_side(outcome.get("name"))
                player = outcome.get("description") or outcome.get("player") or outcome.get("participant")
                if not player and side is None:
                    player = outcome.get("name")
                price = outcome.get("price")
                implied = american_to_implied_probability(price) if odds_format == "american" else decimal_to_implied_probability(price)
                rows.append({
                    "sport": sport_key,
                    "sport_key": sport_key,
                    "league": league,
                    "event_id": event.get("id"),
                    "player": player,
                    "team": outcome.get("team") or _infer_team(player, event),
                    "opponent": _opponent(outcome.get("team"), home_team, away_team),
                    "market_type": market_key,
                    "line": outcome.get("point"),
                    "side": side,
                    "odds": price,
                    "bookmaker": bookmaker.get("title") or bookmaker.get("key"),
                    "bookmaker_key": bookmaker.get("key"),
                    "implied_probability": implied,
                    "commence_time": event.get("commence_time"),
                    "last_update": market.get("last_update") or bookmaker.get("last_update"),
                    "raw_outcome_name": outcome.get("name"),
                    "raw_outcome_description": outcome.get("description"),
                    "raw_price": price,
                })
    return rows


def _normalize_side(value: Any) -> str | None:
    text = str(value or "").strip().lower()
    if text in {"over", "under"}:
        return text
    if text.startswith("over"):
        return "over"
    if text.startswith("under"):
        return "under"
    return None


def _infer_team(player: Any, event: dict[str, Any]) -> str | None:
    return None


def _opponent(team: Any, home: Any, away: Any) -> str | None:
    if not team:
        return None
    if team == home:
        return away
    if team == away:
        return home
    return None
