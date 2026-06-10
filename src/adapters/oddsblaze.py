from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from src.analysis.odds_normalization import american_to_implied_probability

LOGGER = logging.getLogger(__name__)


class OddsBlazeError(RuntimeError):
    pass


class MissingOddsBlazeKey(OddsBlazeError):
    pass


class OddsBlazeClient:
    """Client for OddsBlaze odds endpoint."""

    base_url = "https://odds.oddsblaze.com/"

    def __init__(self, api_key: str | None = None, raw_dir: str | Path = "data/raw", timeout: int = 20) -> None:
        self.api_key = api_key or os.environ.get("ODDSBLAZE_API_KEY")
        self.raw_dir = Path(raw_dir)
        self.timeout = timeout
        self.raw_dir.mkdir(parents=True, exist_ok=True)

    def fetch_odds(
        self,
        sportsbook: str,
        league: str,
        market: str | None = None,
        market_contains: str | None = None,
        main: bool | None = None,
        live: bool | None = None,
        price: str = "american",
    ) -> list[dict[str, Any]]:
        if not self.api_key:
            raise MissingOddsBlazeKey("ODDSBLAZE_API_KEY is required to call OddsBlaze")
        if not sportsbook:
            raise ValueError("sportsbook is required")
        if not league:
            raise ValueError("league is required")
        params: dict[str, Any] = {
            "key": self.api_key,
            "sportsbook": sportsbook,
            "league": league,
            "price": price,
        }
        if market:
            params["market"] = market
        if market_contains:
            params["market_contains"] = market_contains
        if main is not None:
            params["main"] = _bool_param(main)
        if live is not None:
            params["live"] = _bool_param(live)
        payload = self._get_json(params, raw_key=f"{league}_{sportsbook}")
        return normalize_oddsblaze_payload(payload, sportsbook=sportsbook, league=league)

    def _get_json(self, params: dict[str, Any], raw_key: str) -> Any:
        query = urlencode(params)
        url = f"{self.base_url}?{query}"
        safe_url = url.replace(str(self.api_key), "***") if self.api_key else url
        LOGGER.info("api call GET %s", safe_url)
        request = Request(url, headers={"User-Agent": "polylens/0.1", "Accept": "application/json"})
        try:
            with urlopen(request, timeout=self.timeout) as response:
                body = response.read().decode("utf-8")
                payload = json.loads(body) if body else {}
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            self._write_raw(raw_key, {"url": safe_url, "status": exc.code, "error": detail}, error=True)
            raise OddsBlazeError(f"GET OddsBlaze odds failed with HTTP {exc.code}: {detail[:200]}") from exc
        except json.JSONDecodeError as exc:
            self._write_raw(raw_key, {"url": safe_url, "error": "invalid JSON"}, error=True)
            raise OddsBlazeError("GET OddsBlaze odds failed: invalid JSON response") from exc
        except Exception as exc:
            self._write_raw(raw_key, {"url": safe_url, "error": str(exc)}, error=True)
            raise OddsBlazeError(f"GET OddsBlaze odds failed: {exc}") from exc
        self._write_raw(raw_key, {"url": safe_url, "payload": payload})
        return payload if isinstance(payload, dict) else {}

    def _write_raw(self, key: str, payload: Any, error: bool = False) -> None:
        safe_key = re.sub(r"[^a-zA-Z0-9_.-]+", "_", key)[:80]
        status = "error" if error else "raw"
        path = self.raw_dir / f"oddsblaze_{safe_key}_{status}.json"
        path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def normalize_oddsblaze_payload(payload: dict[str, Any], sportsbook: str | None = None, league: str | None = None) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    source_sportsbook = sportsbook or _sportsbook_id(payload.get("sportsbook"))
    source_bookmaker = _sportsbook_name(payload.get("sportsbook")) or source_sportsbook
    source_league = _normalize_league(league or payload.get("league"))
    for event in payload.get("events", []) or []:
        event_id = event.get("id") or event.get("event_id") or event.get("game_id")
        home_team = event.get("home_team") or event.get("home") or event.get("homeTeam")
        away_team = event.get("away_team") or event.get("away") or event.get("awayTeam")
        event_date = event.get("start_time") or event.get("commence_time") or event.get("date") or event.get("event_date")
        for item in event.get("odds", []) or []:
            price = _first_present(item, "price", "american_price", "odds")
            side = _normalize_side(_side_value(item))
            player = _player_name(item)
            line = _line_value(item)
            market = item.get("market") or item.get("market_name") or item.get("market_key")
            row = {
                "provider": "oddsblaze",
                "source": "oddsblaze",
                "sportsbook": source_sportsbook,
                "bookmaker": source_bookmaker,
                "bookmaker_key": source_sportsbook,
                "league": source_league,
                "sport": _sport_key(source_league),
                "sport_key": _sport_key(source_league),
                "event_id": event_id,
                "event_date": event_date,
                "commence_time": event_date,
                "home_team": home_team,
                "away_team": away_team,
                "market": market,
                "market_type": _normalize_market(market),
                "prop_identity": _normalize_market(market),
                "prop_period": _normalize_period(market),
                "player_name": player,
                "player": player,
                "side": side,
                "line": _number_or_original(line),
                "american_price": price,
                "odds": price,
                "implied_probability": american_to_implied_probability(price),
                "deeplink_desktop": _deeplink(item, "desktop"),
                "deeplink_mobile": _deeplink(item, "mobile"),
                "last_update": item.get("updated") or item.get("last_update") or payload.get("updated"),
                "raw": _compact_raw(item),
            }
            rows.append(row)
    return rows


def _bool_param(value: bool) -> str:
    return "true" if bool(value) else "false"


def _first_present(item: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = item.get(key)
        if value is not None and value != "":
            return value
    return None


def _selection(item: dict[str, Any]) -> dict[str, Any]:
    selection = item.get("selection") or {}
    return selection if isinstance(selection, dict) else {}


def _side_value(item: dict[str, Any]) -> Any:
    selection = _selection(item)
    return (
        item.get("side")
        or item.get("selection_side")
        or selection.get("side")
        or selection.get("type")
        or item.get("name")
    )


def _line_value(item: dict[str, Any]) -> Any:
    selection = _selection(item)
    return _first_present(item, "line", "point", "selection_line") or selection.get("line") or selection.get("point")


def _normalize_side(value: Any) -> str | None:
    text = str(value or "").strip().lower()
    if text in {"over", "o"} or text.startswith("over"):
        return "over"
    if text in {"under", "u"} or text.startswith("under"):
        return "under"
    return None


def _player_name(item: dict[str, Any]) -> Any:
    player = item.get("player_name") or item.get("player") or item.get("participant")
    if isinstance(player, dict):
        return player.get("name") or player.get("full_name")
    selection = _selection(item)
    if not player and selection.get("name"):
        player = selection.get("name")
    metadata = item.get("player_metadata") or item.get("playerMeta") or {}
    if isinstance(metadata, dict):
        return player or metadata.get("name") or metadata.get("full_name")
    return player


def _sportsbook_id(value: Any) -> Any:
    if isinstance(value, dict):
        return value.get("id") or value.get("key") or value.get("name")
    return value


def _sportsbook_name(value: Any) -> Any:
    if isinstance(value, dict):
        return value.get("name") or value.get("title") or value.get("id")
    return value


def _normalize_market(value: Any) -> str | None:
    text = str(value or "").strip().lower()
    replacements = {
        "points + rebounds + assists": "player_points_rebounds_assists",
        "points rebounds assists": "player_points_rebounds_assists",
        "points + rebounds": "player_points_rebounds",
        "points rebounds": "player_points_rebounds",
        "points + assists": "player_points_assists",
        "points assists": "player_points_assists",
        "rebounds + assists": "player_rebounds_assists",
        "assists + rebounds": "player_rebounds_assists",
        "rebounds assists": "player_rebounds_assists",
        "assists rebounds": "player_rebounds_assists",
        "blocks + steals": "player_blocks_steals",
        "blocks steals": "player_blocks_steals",
        "points": "player_points",
        "rebounds": "player_rebounds",
        "assists": "player_assists",
        "threes": "player_threes",
        "3-pointers": "player_threes",
        "blocks": "player_blocks",
        "steals": "player_steals",
        "turnovers": "player_turnovers",
    }
    if text.startswith("player_"):
        return text
    for needle, normalized in replacements.items():
        if needle in text:
            return normalized
    return text.replace(" ", "_") if text else None


def _normalize_period(value: Any) -> str:
    text = str(value or "").strip().lower()
    compact = re.sub(r"[^a-z0-9]+", " ", text)
    if "1st quarter" in compact or "first quarter" in compact or compact.startswith("q1 "):
        return "first_quarter"
    if "2nd quarter" in compact or "second quarter" in compact or compact.startswith("q2 "):
        return "second_quarter"
    if "3rd quarter" in compact or "third quarter" in compact or compact.startswith("q3 "):
        return "third_quarter"
    if "4th quarter" in compact or "fourth quarter" in compact or compact.startswith("q4 "):
        return "fourth_quarter"
    if "1st half" in compact or "first half" in compact:
        return "first_half"
    if "2nd half" in compact or "second half" in compact:
        return "second_half"
    return "full_game"


def _normalize_league(value: Any) -> str:
    text = str(value or "").strip()
    return text.upper() if text else ""


def _sport_key(league: str) -> str:
    mapping = {"NBA": "basketball_nba", "NFL": "americanfootball_nfl", "MLB": "baseball_mlb", "NHL": "icehockey_nhl"}
    return mapping.get(str(league or "").upper(), str(league or "").lower())


def _number_or_original(value: Any) -> Any:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return value


def _deeplink(item: dict[str, Any], platform: str) -> Any:
    links = item.get("links") or {}
    if isinstance(links, dict):
        return links.get(platform) or links.get(f"{platform}_url")
    return None


def _compact_raw(item: dict[str, Any]) -> dict[str, Any]:
    keep = ("id", "market", "name", "price", "main", "side", "selection", "line", "player_name", "team")
    return {key: item.get(key) for key in keep if key in item}
