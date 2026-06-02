from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

LOGGER = logging.getLogger(__name__)
WALLET_RE = re.compile(r"^0x[a-fA-F0-9]{40}$")


class PolymarketAPIError(RuntimeError):
    pass


class PolymarketClient:
    """Small public API client for Polymarket Gamma and Data APIs."""

    gamma_base_url = "https://gamma-api.polymarket.com"
    data_base_url = "https://data-api.polymarket.com"

    def __init__(self, raw_dir: str | Path = "data/raw", timeout: int = 20) -> None:
        self.raw_dir = Path(raw_dir)
        self.timeout = timeout
        self.raw_dir.mkdir(parents=True, exist_ok=True)

    def get_user_activity(self, wallet: str, limit: int = 500, max_pages: int = 20) -> list[dict[str, Any]]:
        self._validate_wallet(wallet)
        return self._paginate_data("activity", wallet, limit=min(limit, 500), max_pages=max_pages)

    def get_user_trades(self, wallet: str, limit: int = 1000, max_pages: int = 20) -> list[dict[str, Any]]:
        self._validate_wallet(wallet)
        return self._paginate_data("trades", wallet, limit=min(limit, 10000), max_pages=max_pages)

    def get_public_profile(self, wallet: str) -> dict[str, Any]:
        self._validate_wallet(wallet)
        try:
            return self._get_json(self.gamma_base_url, "public-profile", {"address": wallet}, wallet)
        except PolymarketAPIError as exc:
            LOGGER.warning("public profile unavailable for %s: %s", wallet, exc)
            return {}

    def get_positions(self, wallet: str) -> list[dict[str, Any]]:
        self._validate_wallet(wallet)
        payload = self._get_json(self.data_base_url, "positions", {"user": wallet}, wallet)
        return payload if isinstance(payload, list) else []

    def get_market_by_slug(self, slug: str) -> dict[str, Any]:
        if not slug:
            return {}
        try:
            return self._get_json(self.gamma_base_url, f"markets/slug/{slug}", {}, slug)
        except PolymarketAPIError as exc:
            LOGGER.debug("market lookup failed for %s: %s", slug, exc)
            return {}



    def get_active_markets(
        self,
        keyword: str | None = None,
        category: str | None = None,
        sport: str | None = None,
        limit: int = 100,
        active: bool = True,
    ) -> list[dict[str, Any]]:
        """Fetch active public Gamma markets without requiring a wallet."""
        params: dict[str, Any] = {"limit": max(1, min(int(limit or 100), 1000)), "active": str(active).lower()}
        if keyword:
            params["q"] = keyword
        if category:
            params["category"] = category
        if sport:
            params["tag_slug"] = sport
        payload = self._get_json(self.gamma_base_url, "markets", params, "active_markets")
        rows: Any
        if isinstance(payload, list):
            rows = payload
        elif isinstance(payload, dict):
            rows = payload.get("markets") or payload.get("data") or []
        else:
            rows = []
        if not isinstance(rows, list):
            LOGGER.warning("active markets returned unexpected payload shape")
            return []
        return [self._preserve_live_market_fields(market) for market in rows if isinstance(market, dict)]

    def get_wallet_markets(self, wallet: str, include_market_details: bool = True) -> list[dict[str, Any]]:
        trades = self.get_user_trades(wallet)
        grouped: dict[str, dict[str, Any]] = {}
        for trade in trades:
            key = str(trade.get("conditionId") or trade.get("slug") or trade.get("title") or "unknown")
            item = grouped.setdefault(key, self._preserve_trade_market_fields(trade))
            item["trade_count"] += 1
            item["trades"].append(trade)
        if include_market_details:
            for market in grouped.values():
                details = self.get_market_by_slug(str(market.get("slug") or "")) if market.get("slug") else {}
                if details:
                    market["market_details"] = details
                    market["status"] = details.get("status") or details.get("active") or market.get("status")
                    market["end_date"] = details.get("endDate") or details.get("end_date") or details.get("closeTime") or market.get("end_date")
                    market["resolved"] = details.get("closed") or details.get("resolved") or details.get("archived")
                    market["outcome_prices"] = details.get("outcomePrices") or details.get("outcome_prices")
                    market["token_ids"] = details.get("clobTokenIds") or details.get("tokenIds") or details.get("token_ids") or market.get("token_ids")
        return list(grouped.values())


    @staticmethod
    def _preserve_live_market_fields(market: dict[str, Any]) -> dict[str, Any]:
        preserved = dict(market)
        preserved["condition_id"] = market.get("conditionId") or market.get("condition_id") or market.get("id")
        preserved["market_id"] = market.get("id") or market.get("marketId") or market.get("conditionId")
        preserved["title"] = market.get("question") or market.get("title") or market.get("slug")
        preserved["question"] = market.get("question") or market.get("title")
        preserved["category"] = market.get("category") or market.get("categorySlug") or market.get("groupItemTitle")
        preserved["status"] = market.get("status") or market.get("active")
        preserved["end_date"] = market.get("endDate") or market.get("end_date") or market.get("closeTime") or market.get("end_date_iso")
        preserved["outcome_prices"] = market.get("outcomePrices") or market.get("outcome_prices")
        preserved["outcomes"] = market.get("outcomes")
        preserved["token_ids"] = market.get("clobTokenIds") or market.get("tokenIds") or market.get("token_ids")
        preserved["liquidity"] = market.get("liquidity") or market.get("liquidityNum") or market.get("volumeNum")
        return preserved

    @staticmethod
    def _preserve_trade_market_fields(trade: dict[str, Any]) -> dict[str, Any]:
        token_ids = [trade.get("asset")] if trade.get("asset") else []
        return {
            "condition_id": trade.get("conditionId"),
            "market_id": trade.get("market") or trade.get("marketId") or trade.get("conditionId"),
            "slug": trade.get("slug"),
            "event_slug": trade.get("eventSlug"),
            "question": trade.get("question") or trade.get("title"),
            "title": trade.get("title") or trade.get("question"),
            "status": trade.get("status"),
            "end_date": trade.get("endDate") or trade.get("end_date") or trade.get("closeTime"),
            "resolved": trade.get("resolved") or trade.get("closed"),
            "outcome_prices": trade.get("outcomePrices") or trade.get("outcome_prices"),
            "token_ids": trade.get("clobTokenIds") or trade.get("tokenIds") or token_ids,
            "trade_count": 0,
            "trades": [],
        }

    def _paginate_data(self, endpoint: str, wallet: str, limit: int, max_pages: int) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        offset = 0
        for page in range(max_pages):
            payload = self._get_json(
                self.data_base_url,
                endpoint,
                {"user": wallet, "limit": limit, "offset": offset},
                wallet,
                page=page,
            )
            if not isinstance(payload, list):
                LOGGER.warning("%s returned non-list payload for %s", endpoint, wallet)
                break
            rows.extend(payload)
            LOGGER.info("fetched %s page=%s rows=%s wallet=%s", endpoint, page, len(payload), wallet)
            if len(payload) < limit:
                break
            offset += limit
        return rows

    def _get_json(self, base_url: str, endpoint: str, params: dict[str, Any], raw_key: str, page: int | None = None) -> Any:
        query = urlencode({k: v for k, v in params.items() if v is not None})
        url = f"{base_url.rstrip('/')}/{endpoint.lstrip('/')}"
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
            self._write_raw(endpoint, raw_key, {"url": url, "status": exc.code, "error": detail}, page=page, error=True)
            raise PolymarketAPIError(f"GET {url} failed with HTTP {exc.code}: {detail[:200]}") from exc
        except Exception as exc:  # network and JSON failures should be logged together
            self._write_raw(endpoint, raw_key, {"url": url, "error": str(exc)}, page=page, error=True)
            raise PolymarketAPIError(f"GET {url} failed: {exc}") from exc
        self._write_raw(endpoint, raw_key, {"url": url, "payload": payload}, page=page)
        return payload

    def _write_raw(self, endpoint: str, key: str, payload: Any, page: int | None = None, error: bool = False) -> None:
        safe_endpoint = re.sub(r"[^a-zA-Z0-9_.-]+", "_", endpoint.strip("/"))
        safe_key = re.sub(r"[^a-zA-Z0-9_.-]+", "_", key)[:80]
        suffix = f"_page{page}" if page is not None else ""
        status = "error" if error else "raw"
        path = self.raw_dir / f"{safe_key}_{safe_endpoint}{suffix}_{status}.json"
        with path.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
        LOGGER.debug("stored raw response %s", path)

    @staticmethod
    def _validate_wallet(wallet: str) -> None:
        if not WALLET_RE.match(wallet):
            raise ValueError("wallet must be a 0x-prefixed 40 character hex address")


_default_client = PolymarketClient()


def get_user_activity(wallet: str) -> list[dict[str, Any]]:
    return _default_client.get_user_activity(wallet)


def get_user_trades(wallet: str) -> list[dict[str, Any]]:
    return _default_client.get_user_trades(wallet)


def get_public_profile(wallet: str) -> dict[str, Any]:
    return _default_client.get_public_profile(wallet)


def get_positions(wallet: str) -> list[dict[str, Any]]:
    return _default_client.get_positions(wallet)


def get_active_markets(keyword: str | None = None, category: str | None = None, sport: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
    return _default_client.get_active_markets(keyword=keyword, category=category, sport=sport, limit=limit)
