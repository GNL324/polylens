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

    def list_sports(self) -> list[dict[str, Any]]:
        payload = self._get_json("sports", {}, raw_key="sports")
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
