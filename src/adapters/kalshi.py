from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

LOGGER = logging.getLogger(__name__)


class KalshiAPIError(RuntimeError):
    pass


class KalshiClient:
    """Unauthenticated Kalshi public market-data client with local cache fallback."""

    base_url = "https://external-api.kalshi.com/trade-api/v2"

    def __init__(self, raw_dir: str | Path = "data/raw", timeout: int = 20) -> None:
        self.raw_dir = Path(raw_dir)
        self.timeout = timeout
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        self.cache_path = self.raw_dir / "kalshi_markets_cache.json"

    def get_markets(self, status: str = "open", limit: int = 1000, max_pages: int = 5, use_cache: bool = True) -> list[dict[str, Any]]:
        markets: list[dict[str, Any]] = []
        cursor: str | None = None
        try:
            for page in range(max_pages):
                params: dict[str, Any] = {"status": status, "limit": limit}
                if cursor:
                    params["cursor"] = cursor
                payload = self._get_json("markets", params, page=page)
                page_markets = payload.get("markets", []) if isinstance(payload, dict) else []
                if not isinstance(page_markets, list):
                    LOGGER.warning("Kalshi markets payload had unexpected shape on page %s", page)
                    break
                markets.extend(self._preserve_market_fields(market) for market in page_markets)
                cursor = payload.get("cursor") if isinstance(payload, dict) else None
                LOGGER.info("fetched Kalshi markets page=%s rows=%s", page, len(page_markets))
                if not cursor or not page_markets:
                    break
            self._write_cache(markets)
            return markets
        except KalshiAPIError:
            if use_cache:
                cached = self._read_cache()
                if cached:
                    LOGGER.warning("using cached Kalshi markets after API failure rows=%s", len(cached))
                    return cached
            raise


    @staticmethod
    def _preserve_market_fields(market: dict[str, Any]) -> dict[str, Any]:
        preserved = dict(market)
        preserved["pricing"] = {
            "best_bid": _decimal_price(market.get("yes_bid_dollars") or market.get("yes_bid")),
            "best_ask": _decimal_price(market.get("yes_ask_dollars") or market.get("yes_ask")),
            "last_price": _decimal_price(market.get("last_price_dollars") or market.get("last_price")),
            "yes_bid": _decimal_price(market.get("yes_bid_dollars") or market.get("yes_bid")),
            "yes_ask": _decimal_price(market.get("yes_ask_dollars") or market.get("yes_ask")),
            "no_bid": _decimal_price(market.get("no_bid_dollars") or market.get("no_bid")),
            "no_ask": _decimal_price(market.get("no_ask_dollars") or market.get("no_ask")),
            "liquidity": _decimal_price(market.get("liquidity_dollars") or market.get("liquidity")),
            "yes_bid_size": _decimal_price(market.get("yes_bid_size_fp") or market.get("yes_bid_size")),
            "yes_ask_size": _decimal_price(market.get("yes_ask_size_fp") or market.get("yes_ask_size")),
        }
        preserved["market_identity"] = {
            "ticker": market.get("ticker"),
            "event_ticker": market.get("event_ticker"),
            "title": market.get("title"),
            "subtitle": market.get("subtitle") or market.get("yes_sub_title") or market.get("no_sub_title"),
            "close_time": market.get("close_time"),
            "status": market.get("status"),
        }
        return preserved

    def _get_json(self, endpoint: str, params: dict[str, Any], page: int | None = None) -> Any:
        query = urlencode({key: value for key, value in params.items() if value is not None})
        url = f"{self.base_url.rstrip('/')}/{endpoint.lstrip('/')}"
        if query:
            url = f"{url}?{query}"
        LOGGER.info("api call GET %s", url)
        request = Request(url, headers={"User-Agent": "polylens/0.1", "Accept": "application/json"})
        try:
            with urlopen(request, timeout=self.timeout) as response:
                body = response.read().decode("utf-8")
                payload = json.loads(body) if body else None
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            self._write_raw(endpoint, {"url": url, "status": exc.code, "error": detail}, page=page, error=True)
            raise KalshiAPIError(f"GET {url} failed with HTTP {exc.code}: {detail[:200]}") from exc
        except Exception as exc:
            self._write_raw(endpoint, {"url": url, "error": str(exc)}, page=page, error=True)
            raise KalshiAPIError(f"GET {url} failed: {exc}") from exc
        self._write_raw(endpoint, {"url": url, "payload": payload}, page=page)
        return payload

    def _write_raw(self, endpoint: str, payload: Any, page: int | None = None, error: bool = False) -> None:
        safe_endpoint = re.sub(r"[^a-zA-Z0-9_.-]+", "_", endpoint.strip("/"))
        suffix = f"_page{page}" if page is not None else ""
        status = "error" if error else "raw"
        path = self.raw_dir / f"kalshi_{safe_endpoint}{suffix}_{status}.json"
        path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        LOGGER.debug("stored Kalshi raw response %s", path)

    def _write_cache(self, markets: list[dict[str, Any]]) -> None:
        self.cache_path.write_text(json.dumps({"markets": markets}, indent=2, sort_keys=True), encoding="utf-8")

    def _read_cache(self) -> list[dict[str, Any]]:
        if not self.cache_path.exists():
            return []
        try:
            payload = json.loads(self.cache_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            LOGGER.warning("Kalshi cache is not valid JSON: %s", self.cache_path)
            return []
        markets = payload.get("markets", payload) if isinstance(payload, dict) else payload
        return markets if isinstance(markets, list) else []


def get_markets() -> list[dict[str, Any]]:
    return KalshiClient().get_markets()



def _decimal_price(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    if numeric > 1 and numeric <= 100:
        numeric = numeric / 100
    return round(numeric, 4)
