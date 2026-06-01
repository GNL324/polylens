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
