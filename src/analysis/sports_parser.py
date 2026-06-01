from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any

from src.analysis.market_normalization import normalize_text

TEAM_ALIASES = {
    "NBA": {
        "boston celtics": ["boston celtics", "celtics", "boston"],
        "los angeles lakers": ["los angeles lakers", "la lakers", "lakers", "los angeles"],
        "new york knicks": ["new york knicks", "ny knicks", "knicks", "new york"],
        "oklahoma city thunder": ["oklahoma city thunder", "okc thunder", "thunder"],
        "denver nuggets": ["denver nuggets", "nuggets"],
        "golden state warriors": ["golden state warriors", "warriors"],
        "minnesota timberwolves": ["minnesota timberwolves", "timberwolves"],
        "dallas mavericks": ["dallas mavericks", "mavericks", "mavs"],
    },
    "MLB": {
        "new york yankees": ["new york yankees", "ny yankees", "yankees", "new york"],
        "los angeles dodgers": ["los angeles dodgers", "la dodgers", "dodgers", "los angeles"],
        "boston red sox": ["boston red sox", "red sox"],
        "new york mets": ["new york mets", "ny mets", "mets"],
        "chicago cubs": ["chicago cubs", "cubs"],
        "atlanta braves": ["atlanta braves", "braves"],
        "houston astros": ["houston astros", "astros"],
        "philadelphia phillies": ["philadelphia phillies", "phillies"],
    },
    "NHL": {
        "new york rangers": ["new york rangers", "ny rangers", "rangers", "new york"],
        "edmonton oilers": ["edmonton oilers", "oilers", "edmonton"],
        "toronto maple leafs": ["toronto maple leafs", "maple leafs", "leafs"],
        "florida panthers": ["florida panthers", "panthers"],
        "colorado avalanche": ["colorado avalanche", "avalanche", "avs"],
        "vegas golden knights": ["vegas golden knights", "golden knights"],
        "dallas stars": ["dallas stars", "stars"],
        "boston bruins": ["boston bruins", "bruins"],
    },
}
LEAGUE_TERMS = {
    "NBA": ["nba", "basketball"],
    "MLB": ["mlb", "baseball", "world series"],
    "NHL": ["nhl", "hockey", "stanley cup"],
    "NFL": ["nfl", "football", "super bowl"],
}
EVENT_TYPES = {
    "finals": ["finals", "championship", "champion", "title"],
    "world_series": ["world series"],
    "stanley_cup": ["stanley cup"],
    "game": [" vs ", " at ", " game", "matchup", "beat", "defeat"],
    "playoffs": ["playoffs", "postseason"],
}
MARKET_TYPES = {
    "championship_winner": ["win the finals", "win finals", "win championship", "champion", "title", "world series", "stanley cup"],
    "game_winner": ["win tonight", "win the game", "beat", "defeat", "moneyline", " vs ", " at "],
    "series_winner": ["win series", "series winner"],
    "playoff_qualification": ["make playoffs", "make the playoffs", "reach playoffs"],
}


@dataclass(frozen=True)
class ParsedSportsMarket:
    league: str | None
    team: str | None
    opponent: str | None
    season_year: int | None
    event_type: str | None
    market_type: str | None
    matched_terms: list[str]
    raw_text: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def parse_sports_market_text(text: str) -> ParsedSportsMarket:
    normalized = f" {normalize_text(text)} "
    league = _detect_league(normalized)
    teams = _detect_teams(normalized, league)
    if not league and teams:
        league = teams[0][0]
        teams = _detect_teams(normalized, league)
    team = teams[0][1] if teams else None
    opponent = teams[1][1] if len(teams) > 1 else None
    season_year = _detect_year(normalized)
    event_type = _detect_from_terms(normalized, EVENT_TYPES)
    market_type = _detect_market_type(normalized, event_type)
    matched_terms = []
    if league:
        matched_terms.append(league.lower())
    matched_terms.extend(_team_short_terms(team, opponent))
    if season_year:
        matched_terms.append(str(season_year))
    if event_type:
        matched_terms.append(event_type)
    if market_type:
        matched_terms.append(market_type)
    return ParsedSportsMarket(league, team, opponent, season_year, event_type, market_type, matched_terms, text)


def parse_market_record(record: dict[str, Any], source: str) -> ParsedSportsMarket:
    if source == "kalshi":
        fields = ("title", "subtitle", "yes_sub_title", "no_sub_title", "rules_primary", "rules_secondary", "event_ticker", "series_ticker", "ticker")
    else:
        fields = ("title", "slug", "eventSlug", "outcome")
    text = " ".join(str(record.get(field) or "") for field in fields)
    return parse_sports_market_text(text)


def _detect_market_type(normalized: str, event_type: str | None) -> str | None:
    detected = _detect_from_terms(normalized, MARKET_TYPES)
    if detected:
        return detected
    if event_type in {"finals", "world_series", "stanley_cup"} and " win " in normalized:
        return "championship_winner"
    return None


def _detect_league(normalized: str) -> str | None:
    for league, terms in LEAGUE_TERMS.items():
        if any(f" {normalize_text(term)} " in normalized for term in terms):
            return league
    return None


def _detect_teams(normalized: str, league: str | None) -> list[tuple[str, str]]:
    leagues = [league] if league in TEAM_ALIASES else TEAM_ALIASES.keys()
    matches: list[tuple[int, str, str]] = []
    for candidate_league in leagues:
        for canonical, aliases in TEAM_ALIASES[candidate_league].items():
            for alias in aliases:
                alias_text = f" {normalize_text(alias)} "
                index = normalized.find(alias_text)
                if index >= 0:
                    matches.append((index, candidate_league, canonical))
                    break
    matches.sort(key=lambda item: item[0])
    deduped: list[tuple[str, str]] = []
    seen: set[str] = set()
    for _index, item_league, team in matches:
        if team not in seen:
            deduped.append((item_league, team))
            seen.add(team)
    return deduped


def _detect_year(normalized: str) -> int | None:
    match = re.search(r"\b(20\d{2})\b", normalized)
    if match:
        return int(match.group(1))
    return None


def _detect_from_terms(normalized: str, lookup: dict[str, list[str]]) -> str | None:
    for value, terms in lookup.items():
        if any(f" {normalize_text(term)} " in normalized for term in terms):
            return value
    return None


def _team_short_terms(team: str | None, opponent: str | None) -> list[str]:
    terms = []
    for value in (team, opponent):
        if value:
            terms.extend(value.split())
    return sorted(set(terms))
