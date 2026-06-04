from __future__ import annotations

import base64
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

LOGGER = logging.getLogger(__name__)


class KalshiAPIError(RuntimeError):
    pass


class KalshiAuthConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class KalshiAuthConfig:
    api_key_id: str
    private_key_path: Path
    env: str = "demo"
    base_url: str | None = None

    @classmethod
    def from_env(cls, env_path: str = ".env") -> "KalshiAuthConfig":
        _load_env_file(env_path)
        api_key_id = (os.environ.get("KALSHI_API_KEY_ID") or "").strip()
        private_key_path = (os.environ.get("KALSHI_PRIVATE_KEY_PATH") or "").strip()
        env = (os.environ.get("KALSHI_ENV") or "demo").strip().lower()
        base_url = (os.environ.get("KALSHI_BASE_URL") or "").strip() or None
        if not api_key_id:
            raise KalshiAuthConfigError("KALSHI_API_KEY_ID is required for authenticated Kalshi reads")
        if not private_key_path:
            raise KalshiAuthConfigError("KALSHI_PRIVATE_KEY_PATH is required for authenticated Kalshi reads")
        if env not in {"demo", "production"}:
            raise KalshiAuthConfigError("KALSHI_ENV must be demo or production")
        path = Path(private_key_path).expanduser()
        if not path.exists():
            raise KalshiAuthConfigError(f"Kalshi private key file not found: {path}")
        if not path.is_file():
            raise KalshiAuthConfigError(f"Kalshi private key path is not a file: {path}")
        return cls(api_key_id=api_key_id, private_key_path=path, env=env, base_url=base_url)

    def resolved_base_url(self) -> str:
        if self.base_url:
            return self.base_url.rstrip("/")
        if self.env == "production":
            return "https://external-api.kalshi.com/trade-api/v2"
        return "https://external-api.demo.kalshi.co/trade-api/v2"


class KalshiClient:
    """Unauthenticated Kalshi public market-data client with local cache fallback."""

    base_url = "https://external-api.kalshi.com/trade-api/v2"

    def __init__(self, raw_dir: str | Path = "data/raw", timeout: int = 20) -> None:
        self.raw_dir = Path(raw_dir)
        self.timeout = timeout
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        self.cache_path = self.raw_dir / "kalshi_markets_cache.json"

    def get_markets(self, status: str = "open", limit: int = 1000, max_pages: int = 5, use_cache: bool = True, include_closed: bool = False) -> list[dict[str, Any]]:
        statuses = [status]
        if include_closed and status == "open":
            statuses = ["open", "closed", "settled"]
        markets: list[dict[str, Any]] = []
        try:
            for status_value in statuses:
                try:
                    markets.extend(self._get_markets_for_status(status_value, limit=limit, max_pages=max_pages))
                except KalshiAPIError:
                    if include_closed and status_value != status:
                        LOGGER.warning("Kalshi status=%s unavailable; continuing with fetched markets", status_value)
                        continue
                    raise
            self._write_cache(markets)
            return markets
        except KalshiAPIError:
            if use_cache:
                cached = self._read_cache()
                if cached:
                    LOGGER.warning("using cached Kalshi markets after API failure rows=%s", len(cached))
                    return cached
            raise

    def _get_markets_for_status(self, status: str, limit: int, max_pages: int) -> list[dict[str, Any]]:
        markets: list[dict[str, Any]] = []
        cursor: str | None = None
        for page in range(max_pages):
            params: dict[str, Any] = {"status": status, "limit": limit}
            if cursor:
                params["cursor"] = cursor
            payload = self._get_json("markets", params, page=page)
            page_markets = payload.get("markets", []) if isinstance(payload, dict) else []
            if not isinstance(page_markets, list):
                LOGGER.warning("Kalshi markets payload had unexpected shape on page %s status=%s", page, status)
                break
            markets.extend(self._preserve_market_fields(market) for market in page_markets)
            cursor = payload.get("cursor") if isinstance(payload, dict) else None
            LOGGER.info("fetched Kalshi markets status=%s page=%s rows=%s", status, page, len(page_markets))
            if not cursor or not page_markets:
                break
        return markets

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
            "category": market.get("category"),
            "status": market.get("status"),
            "open_time": market.get("open_time"),
            "close_time": market.get("close_time"),
            "expiration_time": market.get("expiration_time") or market.get("expected_expiration_time"),
        }
        return preserved

    def get_orderbook(self, ticker: str) -> dict[str, Any]:
        if not ticker:
            raise ValueError("ticker is required")
        payload = self._get_json(f"markets/{ticker}/orderbook", {}, page=None)
        return payload if isinstance(payload, dict) else {}

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


class KalshiAuthenticatedClient:
    """Authenticated read-only Kalshi client."""

    def __init__(self, config: KalshiAuthConfig | None = None, raw_dir: str | Path = "data/raw", timeout: int = 20) -> None:
        self.config = config or KalshiAuthConfig.from_env()
        self.raw_dir = Path(raw_dir)
        self.timeout = timeout
        self.base_url = self.config.resolved_base_url()
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        self._private_key: Any | None = None

    def get_account(self) -> dict[str, Any]:
        return self._get_authenticated_json("account/limits")

    def get_balance(self) -> dict[str, Any]:
        return self._get_authenticated_json("portfolio/balance")

    def get_positions(self, limit: int = 100, max_pages: int = 5, **filters: Any) -> dict[str, Any]:
        return self._get_paginated("portfolio/positions", "positions", limit=limit, max_pages=max_pages, **filters)

    def get_orders(self, limit: int = 100, max_pages: int = 5, **filters: Any) -> dict[str, Any]:
        return self._get_paginated("portfolio/orders", "orders", limit=limit, max_pages=max_pages, **filters)

    def get_fills(self, limit: int = 100, max_pages: int = 5, **filters: Any) -> dict[str, Any]:
        return self._get_paginated("portfolio/fills", "fills", limit=limit, max_pages=max_pages, **filters)

    def place_order(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return self.write_blocked("place_order")

    def cancel_order(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return self.write_blocked("cancel_order")

    def write_blocked(self, operation: str) -> dict[str, Any]:
        return {"accepted": False, "mode": "write_blocked", "operation": operation, "reason": "Kalshi live order writes are disabled"}

    def _get_paginated(self, endpoint: str, collection_key: str, limit: int = 100, max_pages: int = 5, **filters: Any) -> dict[str, Any]:
        rows: list[dict[str, Any]] = []
        cursor: str | None = None
        last_payload: dict[str, Any] = {}
        pages_fetched = 0
        for page in range(max_pages):
            params = {"limit": max(1, min(int(limit), 1000)), **{key: value for key, value in filters.items() if value is not None}}
            if cursor:
                params["cursor"] = cursor
            payload = self._get_authenticated_json(endpoint, params=params, page=page)
            pages_fetched += 1
            last_payload = payload if isinstance(payload, dict) else {}
            page_rows = last_payload.get(collection_key, [])
            if isinstance(page_rows, list):
                rows.extend(page_rows)
            cursor = last_payload.get("cursor")
            if not cursor or not page_rows:
                break
        result = dict(last_payload)
        result[collection_key] = rows
        result["pages_fetched"] = pages_fetched
        return result

    def _get_authenticated_json(self, endpoint: str, params: dict[str, Any] | None = None, page: int | None = None) -> dict[str, Any]:
        return self._request_authenticated_json("GET", endpoint, params=params or {}, page=page)

    def _request_authenticated_json(self, method: str, endpoint: str, params: dict[str, Any] | None = None, page: int | None = None) -> dict[str, Any]:
        method = method.upper()
        if method != "GET":
            return self.write_blocked(f"{method} {endpoint}")
        params = params or {}
        query = urlencode({key: value for key, value in params.items() if value is not None})
        url = f"{self.base_url.rstrip('/')}/{endpoint.lstrip('/')}"
        if query:
            url = f"{url}?{query}"
        timestamp = str(int(time.time() * 1000))
        sign_path = urlparse(url).path
        headers = {
            "User-Agent": "polylens/0.1",
            "Accept": "application/json",
            "KALSHI-ACCESS-KEY": self.config.api_key_id,
            "KALSHI-ACCESS-TIMESTAMP": timestamp,
            "KALSHI-ACCESS-SIGNATURE": self._sign_request(timestamp, method, sign_path),
        }
        LOGGER.info("authenticated Kalshi api call GET %s", _redact_url(url))
        request = Request(url, headers=headers)
        try:
            with urlopen(request, timeout=self.timeout) as response:
                body = response.read().decode("utf-8")
                payload = json.loads(body) if body else {}
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            self._write_raw(endpoint, {"url": _redact_url(url), "status": exc.code, "error": detail}, page=page, error=True)
            if exc.code == 401:
                raise KalshiAPIError("authenticated Kalshi GET failed with HTTP 401: check KALSHI_API_KEY_ID, key file, environment, and clock skew") from exc
            raise KalshiAPIError(f"authenticated Kalshi GET failed with HTTP {exc.code}: {detail[:200]}") from exc
        except Exception as exc:
            self._write_raw(endpoint, {"url": _redact_url(url), "error": str(exc)}, page=page, error=True)
            raise KalshiAPIError(f"authenticated Kalshi GET failed: {exc}") from exc
        self._write_raw(endpoint, {"url": _redact_url(url), "payload": payload}, page=page)
        return payload if isinstance(payload, dict) else {"payload": payload}

    def _sign_request(self, timestamp: str, method: str, path: str) -> str:
        private_key = self._private_key or self._load_private_key()
        self._private_key = private_key
        path_without_query = path.split("?", 1)[0]
        message = f"{timestamp}{method.upper()}{path_without_query}".encode("utf-8")
        try:
            from cryptography.hazmat.primitives import hashes
            from cryptography.hazmat.primitives.asymmetric import padding
        except ImportError as exc:
            raise KalshiAuthConfigError("cryptography is required for Kalshi authenticated requests; install it in the Polylens virtual environment") from exc
        signature = private_key.sign(
            message,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
            hashes.SHA256(),
        )
        return base64.b64encode(signature).decode("utf-8")

    def _load_private_key(self) -> Any:
        try:
            from cryptography.hazmat.primitives import serialization
            from cryptography.hazmat.backends import default_backend
        except ImportError as exc:
            raise KalshiAuthConfigError("cryptography is required for Kalshi authenticated requests; install it in the Polylens virtual environment") from exc
        try:
            key_bytes = self.config.private_key_path.read_bytes()
            return serialization.load_pem_private_key(key_bytes, password=None, backend=default_backend())
        except Exception as exc:
            raise KalshiAuthConfigError(f"failed to load Kalshi private key from {self.config.private_key_path}: {exc}") from exc

    def _write_raw(self, endpoint: str, payload: Any, page: int | None = None, error: bool = False) -> None:
        safe_endpoint = re.sub(r"[^a-zA-Z0-9_.-]+", "_", endpoint.strip("/"))
        suffix = f"_page{page}" if page is not None else ""
        status = "error" if error else "raw"
        path = self.raw_dir / f"kalshi_auth_{safe_endpoint}{suffix}_{status}.json"
        path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        LOGGER.debug("stored authenticated Kalshi response %s", path)


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


def _load_env_file(path: str = ".env") -> dict[str, str]:
    values: dict[str, str] = {}
    env_path = Path(path)
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, value = stripped.split("=", 1)
            values[key.strip()] = value.strip().strip('"').strip("'")
    for key, value in values.items():
        os.environ.setdefault(key, value)
    return values


def _redact_url(url: str) -> str:
    parsed = urlparse(url)
    return parsed._replace(query="<redacted>" if parsed.query else "").geturl()
