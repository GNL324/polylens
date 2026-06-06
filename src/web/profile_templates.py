from __future__ import annotations

from typing import Any


DEFAULT_BANKROLL = 1000.0
DEFAULT_INTERVAL_SECONDS = 60
DEFAULT_MIN_ROI = 0.0


SCANNER_PROFILE_TEMPLATES: tuple[dict[str, Any], ...] = (
    {"template_id": "nba_points_all", "name": "NBA Points - All Books", "sport": "basketball_nba", "markets": ["player_points"], "sportsbooks": [], "recommended": True},
    {"template_id": "nba_rebounds_all", "name": "NBA Rebounds - All Books", "sport": "basketball_nba", "markets": ["player_rebounds"], "sportsbooks": [], "recommended": False},
    {"template_id": "nba_assists_all", "name": "NBA Assists - All Books", "sport": "basketball_nba", "markets": ["player_assists"], "sportsbooks": [], "recommended": False},
    {"template_id": "nba_points_rebounds", "name": "NBA Points + Rebounds", "sport": "basketball_nba", "markets": ["player_points", "player_rebounds"], "sportsbooks": [], "recommended": False},
    {"template_id": "nba_points_fanduel_bovada", "name": "NBA Points - FanDuel/Bovada", "sport": "basketball_nba", "markets": ["player_points"], "sportsbooks": ["fanduel", "bovada"], "recommended": False},
    {"template_id": "wnba_points_all", "name": "WNBA Points - All Books", "sport": "basketball_wnba", "markets": ["player_points"], "sportsbooks": [], "recommended": False},
    {"template_id": "wnba_rebounds_all", "name": "WNBA Rebounds - All Books", "sport": "basketball_wnba", "markets": ["player_rebounds"], "sportsbooks": [], "recommended": False},
    {"template_id": "wnba_assists_all", "name": "WNBA Assists - All Books", "sport": "basketball_wnba", "markets": ["player_assists"], "sportsbooks": [], "recommended": False},
    {"template_id": "mlb_home_runs", "name": "MLB Home Runs", "sport": "baseball_mlb", "markets": ["batter_home_runs"], "sportsbooks": [], "recommended": False},
    {"template_id": "mlb_rbis", "name": "MLB RBIs", "sport": "baseball_mlb", "markets": ["batter_rbis"], "sportsbooks": [], "recommended": False},
    {"template_id": "mlb_hits", "name": "MLB Hits", "sport": "baseball_mlb", "markets": ["batter_hits"], "sportsbooks": [], "recommended": False},
    {"template_id": "mlb_total_bases", "name": "MLB Total Bases", "sport": "baseball_mlb", "markets": ["batter_total_bases"], "sportsbooks": [], "recommended": False},
    {"template_id": "mlb_pitcher_strikeouts", "name": "MLB Pitcher Strikeouts", "sport": "baseball_mlb", "markets": ["pitcher_strikeouts"], "sportsbooks": [], "recommended": True},
    {"template_id": "nfl_passing_yards", "name": "NFL Passing Yards", "sport": "americanfootball_nfl", "markets": ["player_pass_yds"], "sportsbooks": [], "recommended": False},
    {"template_id": "nfl_passing_tds", "name": "NFL Passing TDs", "sport": "americanfootball_nfl", "markets": ["player_pass_tds"], "sportsbooks": [], "recommended": False},
    {"template_id": "nfl_rushing_yards", "name": "NFL Rushing Yards", "sport": "americanfootball_nfl", "markets": ["player_rush_yds"], "sportsbooks": [], "recommended": False},
    {"template_id": "nfl_receiving_yards", "name": "NFL Receiving Yards", "sport": "americanfootball_nfl", "markets": ["player_reception_yds"], "sportsbooks": [], "recommended": False},
    {"template_id": "nfl_anytime_td", "name": "NFL Anytime TD", "sport": "americanfootball_nfl", "markets": ["player_anytime_td"], "sportsbooks": [], "recommended": True},
    {"template_id": "nhl_goals", "name": "NHL Goals", "sport": "icehockey_nhl", "markets": ["player_goals"], "sportsbooks": [], "recommended": False},
    {"template_id": "nhl_assists", "name": "NHL Assists", "sport": "icehockey_nhl", "markets": ["player_assists"], "sportsbooks": [], "recommended": False},
    {"template_id": "nhl_shots_on_goal", "name": "NHL Shots on Goal", "sport": "icehockey_nhl", "markets": ["player_shots_on_goal"], "sportsbooks": [], "recommended": True},
    {"template_id": "nhl_goalie_saves", "name": "NHL Goalie Saves", "sport": "icehockey_nhl", "markets": ["goalie_saves"], "sportsbooks": [], "recommended": False},
)


def scanner_profile_templates() -> list[dict[str, Any]]:
    templates: list[dict[str, Any]] = []
    for template in SCANNER_PROFILE_TEMPLATES:
        row = dict(template)
        row.setdefault("bankroll", DEFAULT_BANKROLL)
        row.setdefault("interval_seconds", DEFAULT_INTERVAL_SECONDS)
        row.setdefault("min_roi", DEFAULT_MIN_ROI)
        templates.append(row)
    return templates


def get_scanner_profile_template(template_id: str) -> dict[str, Any] | None:
    for template in scanner_profile_templates():
        if template["template_id"] == template_id:
            return template
    return None


def templates_by_sport() -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for template in scanner_profile_templates():
        grouped.setdefault(template["sport"], []).append(template)
    return grouped
