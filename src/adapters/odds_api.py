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

LOGGER = logging.getLogger(__name__)

PLAYER_PROP_MARKETS = (
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
)


class OddsAPIError(RuntimeError):
    pass


class MissingOddsAPIKey(OddsAPIError):
    pass


class OddsAPIClient:
    """Client for The Odds API v4."""

    base_url = "https://api.the-odds-api.com/v4"

    def __init__(self, api_key: str | None = None, raw_dir: str | Path = "data/raw", timeout: int = 20) -> None:
        self.api_key = api_key or os.environ.get("ODDS_API_KEY")
        self.raw_dir = Path(raw_dir)
        self.timeout = timeout
        self.raw_dir.mkdir(parents=True, exist_ok=True)

    def list_sports(self, all_sports: bool = False) -> list[dict[str, Any]]:
        params = {"all": "true"} if all_sports else {}
        payload = self._get_json("sports", params, raw_key="sports_all" if all_sports else "sports")
        return payload if isinstance(payload, list) else []

    def get_events(self, sport_key: str) -> list[dict[str, Any]]:
        if not sport_key:
            raise ValueError("sport_key is required")
        payload = self._get_json(f"sports/{sport_key}/events", {}, raw_key=f"events_{sport_key}")
        return payload if isinstance(payload, list) else []

    def get_odds(
        self,
        sport_key: str,
        regions: str = "us",
        markets: str = "h2h,spreads,totals,outrights",
        bookmakers: str | None = None,
        odds_format: str = "american",
    ) -> list[dict[str, Any]]:
        if not sport_key:
            raise ValueError("sport_key is required")
        params: dict[str, Any] = {
            "regions": regions,
            "markets": markets,
            "oddsFormat": odds_format,
        }
        if bookmakers:
            params["bookmakers"] = bookmakers
        payload = self._get_json(f"sports/{sport_key}/odds", params, raw_key=f"odds_{sport_key}")
        return payload if isinstance(payload, list) else []


    def get_event_props(
        self,
        sport_key: str,
        event_id: str,
        regions: str = "us",
        markets: str | None = None,
        bookmakers: str | None = None,
        odds_format: str = "american",
    ) -> dict[str, Any]:
        if not sport_key or not event_id:
            raise ValueError("sport_key and event_id are required")
        market_list = markets or ",".join(PLAYER_PROP_MARKETS)
        params: dict[str, Any] = {"regions": regions, "markets": market_list, "oddsFormat": odds_format}
        if bookmakers:
            params["bookmakers"] = bookmakers
        try:
            payload = self._get_json(f"sports/{sport_key}/events/{event_id}/odds", params, raw_key=f"player_props_{sport_key}_{event_id}")
            return {"supported": True, "event_id": event_id, "events": [payload] if isinstance(payload, dict) else [], "unsupported_markets": [], "failed_markets": []}
        except OddsAPIError as exc:
            if _is_unsupported_market_error(exc):
                return {"supported": False, "reason": "market unsupported", "event_id": event_id, "events": [], "unsupported_markets": [{"supported": False, "reason": "market unsupported", "market": market} for market in _market_list(market_list)], "failed_markets": []}
            raise

    def get_player_props(
        self,
        sport_key: str,
        event_id: str | None = None,
        regions: str = "us",
        markets: str | None = None,
        bookmakers: str | None = None,
        odds_format: str = "american",
    ) -> dict[str, Any]:
        market_list = _market_list(markets or ",".join(PLAYER_PROP_MARKETS))
        events = [{"id": event_id}] if event_id else self.get_events(sport_key)
        aggregated: list[dict[str, Any]] = []
        unsupported: list[dict[str, Any]] = []
        failed: list[dict[str, Any]] = []
        events_scanned = 0
        for event in events:
            current_event_id = str(event.get("id") or "")
            if not current_event_id:
                continue
            events_scanned += 1
            result = self.get_event_props(sport_key, current_event_id, regions=regions, markets=",".join(market_list), bookmakers=bookmakers, odds_format=odds_format)
            if result.get("supported"):
                aggregated.extend(result.get("events") or [])
                continue
            # Fallback: isolate unsupported markets so supported props still flow.
            for market in market_list:
                single = self.get_event_props(sport_key, current_event_id, regions=regions, markets=market, bookmakers=bookmakers, odds_format=odds_format)
                if single.get("supported"):
                    aggregated.extend(single.get("events") or [])
                else:
                    unsupported.extend(single.get("unsupported_markets") or [{"supported": False, "reason": "market unsupported", "market": market}])
            if result.get("reason") and not result.get("unsupported_markets"):
                failed.append({"event_id": current_event_id, "reason": result.get("reason")})
        return {
            "events": aggregated,
            "events_discovered": len(events),
            "events_scanned": events_scanned,
            "events_failed": len(failed),
            "prop_markets_supported": sorted({market.get("key") for event in aggregated for bookmaker in event.get("bookmakers", []) or [] for market in bookmaker.get("markets", []) or [] if market.get("key")} ),
            "prop_markets_rejected": unsupported,
            "failed_events": failed,
        }

    def get_futures(
        self,
        sport_key: str,
        regions: str = "us",
        bookmakers: str | None = None,
        odds_format: str = "american",
    ) -> dict[str, Any]:
        """Fetch sportsbook futures/outrights using an outright-capable sport key."""
        outright_key = self.find_futures_sport_key(sport_key)
        if not outright_key:
            return {"supported": False, "reason": "no futures endpoint for sport", "events": [], "sport_key": sport_key, "futures_sport_key": None}
        try:
            events = self.get_odds(
                outright_key,
                regions=regions,
                markets="outrights",
                bookmakers=bookmakers,
                odds_format=odds_format,
            )
        except OddsAPIError as exc:
            if "INVALID_MARKET_COMBO" in str(exc) or "422" in str(exc):
                return {"supported": False, "reason": "no futures endpoint for sport", "events": [], "sport_key": sport_key, "futures_sport_key": outright_key, "error": str(exc)}
            raise
        return {"supported": True, "reason": None, "events": events, "sport_key": sport_key, "futures_sport_key": outright_key}

    def find_futures_sport_key(self, sport_key: str) -> str | None:
        sports = self.list_sports(all_sports=True)
        requested = str(sport_key or "")
        for sport in sports:
            if sport.get("key") == requested and sport.get("has_outrights"):
                return requested
        requested_terms = _sport_lookup_terms(requested)
        candidates = [sport for sport in sports if sport.get("has_outrights")]
        for sport in candidates:
            key = str(sport.get("key") or "")
            title = str(sport.get("title") or "")
            description = str(sport.get("description") or "")
            haystack = f"{key} {title} {description}".lower()
            if all(term in haystack for term in requested_terms):
                return key
        return None

    def _get_json(self, endpoint: str, params: dict[str, Any], raw_key: str) -> Any:
        if not self.api_key:
            raise MissingOddsAPIKey("ODDS_API_KEY is required to call The Odds API")
        query = urlencode({**params, "apiKey": self.api_key})
        url = f"{self.base_url.rstrip('/')}/{endpoint.lstrip('/')}?{query}"
        LOGGER.info("api call GET %s", url.replace(self.api_key, "***"))
        request = Request(url, headers={"User-Agent": "polylens/0.1", "Accept": "application/json"})
        try:
            with urlopen(request, timeout=self.timeout) as response:
                body = response.read().decode("utf-8")
                payload = json.loads(body) if body else None
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            self._write_raw(raw_key, {"url": url.replace(self.api_key, "***"), "status": exc.code, "error": detail}, error=True)
            raise OddsAPIError(f"GET {endpoint} failed with HTTP {exc.code}: {detail[:200]}") from exc
        except Exception as exc:
            self._write_raw(raw_key, {"url": url.replace(self.api_key, "***"), "error": str(exc)}, error=True)
            raise OddsAPIError(f"GET {endpoint} failed: {exc}") from exc
        self._write_raw(raw_key, {"url": url.replace(self.api_key, "***"), "payload": payload})
        return payload

    def _write_raw(self, key: str, payload: Any, error: bool = False) -> None:
        safe_key = re.sub(r"[^a-zA-Z0-9_.-]+", "_", key)[:80]
        status = "error" if error else "raw"
        path = self.raw_dir / f"odds_api_{safe_key}_{status}.json"
        path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def _sport_lookup_terms(sport_key: str) -> list[str]:
    mapping = {
        "basketball_nba": ["nba"],
        "americanfootball_nfl": ["nfl"],
        "baseball_mlb": ["mlb"],
        "icehockey_nhl": ["nhl"],
    }
    return mapping.get(sport_key, [part for part in sport_key.lower().split("_") if part])


def _market_list(markets: str | None) -> list[str]:
    return [market.strip() for market in str(markets or "").split(",") if market.strip()]


def _is_unsupported_market_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return "422" in text and ("unsupported" in text or "invalid_market" in text or "invalid_market_combo" in text or "markets not supported" in text)
