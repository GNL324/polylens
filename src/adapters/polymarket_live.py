from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode
from urllib.error import HTTPError
from urllib.request import Request, urlopen


BUY = "BUY"
DEFAULT_CLOB_HOST = "https://clob.polymarket.com"
DEFAULT_GAMMA_HOST = "https://gamma-api.polymarket.com"


@dataclass(frozen=True)
class PolymarketLiveConfig:
    private_key: str | None
    api_key: str | None
    api_secret: str | None
    api_passphrase: str | None
    funder: str | None
    signature_type: int | None
    chain_id: int
    clob_host: str

    @classmethod
    def from_env(cls) -> "PolymarketLiveConfig":
        return cls(
            private_key=_clean(os.environ.get("POLYMARKET_PRIVATE_KEY")),
            api_key=_clean(os.environ.get("POLYMARKET_API_KEY")),
            api_secret=_clean(os.environ.get("POLYMARKET_API_SECRET")),
            api_passphrase=_clean(os.environ.get("POLYMARKET_API_PASSPHRASE")),
            funder=_clean(os.environ.get("POLYMARKET_FUNDER")),
            signature_type=_int_or_none(os.environ.get("POLYMARKET_SIGNATURE_TYPE")),
            chain_id=int(os.environ.get("POLYMARKET_CHAIN_ID") or "137"),
            clob_host=(os.environ.get("POLYMARKET_CLOB_HOST") or DEFAULT_CLOB_HOST).rstrip("/"),
        )


class PolymarketLiveAdapter:
    def __init__(self, config: PolymarketLiveConfig | None = None, timeout: int = 10) -> None:
        self.config = config or PolymarketLiveConfig.from_env()
        self.timeout = timeout
        self._post_order_called = False

    def credentials_present(self) -> dict[str, Any]:
        cfg = self.config
        complete = bool(cfg.private_key and cfg.api_key and cfg.api_secret and cfg.api_passphrase)
        return {
            "ok": complete,
            "credentials_detected_not_printed": complete,
            "private_key_present": bool(cfg.private_key),
            "api_key_present": bool(cfg.api_key),
            "api_secret_present": bool(cfg.api_secret),
            "api_passphrase_present": bool(cfg.api_passphrase),
            "funder_detected_not_printed": bool(cfg.funder),
            "signature_type": cfg.signature_type,
            "chain_id": cfg.chain_id,
            "clob_host": cfg.clob_host,
        }

    def build_client(self) -> Any:
        cfg = self.config
        creds = self.credentials_present()
        if not creds["ok"]:
            raise PolymarketLiveError("polymarket_credentials_missing")
        sdk = _load_sdk()
        if sdk is None:
            raise PolymarketLiveError("polymarket_live_signing_not_ready")
        package, client_cls, creds_cls, _order_args_cls, _order_type_cls, _options_cls = sdk
        if package == "py_clob_client_v2":
            api_creds = creds_cls(api_key=cfg.api_key, api_secret=cfg.api_secret, api_passphrase=cfg.api_passphrase)
            return client_cls(
                cfg.clob_host,
                key=cfg.private_key,
                chain_id=cfg.chain_id,
                creds=api_creds,
                signature_type=cfg.signature_type,
                funder=cfg.funder,
            )
        api_creds = creds_cls(api_key=cfg.api_key, api_secret=cfg.api_secret, api_passphrase=cfg.api_passphrase)
        client = client_cls(cfg.clob_host, key=cfg.private_key, chain_id=cfg.chain_id, signature_type=cfg.signature_type, funder=cfg.funder)
        client.set_api_creds(api_creds)
        return client

    def get_balance_allowance(self) -> dict[str, Any]:
        try:
            client = self.build_client()
        except PolymarketLiveError as exc:
            return {"ok": False, "status": "not_ready", "reason": str(exc)}
        for method_name in ("get_balance_allowance", "get_balance_and_allowance"):
            method = getattr(client, method_name, None)
            if callable(method):
                try:
                    raw = method()
                    return {"ok": True, "status": "ok", "raw": _redact(raw)}
                except Exception as exc:
                    return {"ok": False, "status": "not_ready", "reason": "balance_allowance_check_failed", "detail": str(exc)}
        return {"ok": False, "status": "not_ready", "reason": "balance_allowance_method_not_available"}

    def create_signed_order(self, *, token_id: str, price: float, size: float, side: str = BUY, tick_size: str | None = None, neg_risk: bool | None = None) -> dict[str, Any]:
        order_args = self.order_args(token_id=token_id, price=price, size=size, side=side)
        try:
            client = self.build_client()
            sdk = _load_sdk()
            if sdk is None:
                raise PolymarketLiveError("polymarket_live_signing_not_ready")
            _package, _client_cls, _creds_cls, order_args_cls, _order_type_cls, options_cls = sdk
            sdk_args = order_args_cls(token_id=token_id, price=float(price), size=float(size), side=side)
            if hasattr(client, "create_order"):
                if options_cls and tick_size is not None and neg_risk is not None:
                    signed = client.create_order(sdk_args, options=options_cls(tick_size=str(tick_size), neg_risk=bool(neg_risk)))
                else:
                    signed = client.create_order(sdk_args)
            else:
                raise PolymarketLiveError("polymarket_live_signing_not_ready")
            return {"ok": True, "status": "signed", "order_args": order_args, "signed_order": _redact(signed)}
        except PolymarketLiveError as exc:
            return {"ok": False, "status": "blocked", "reason": str(exc), "order_args": order_args}
        except Exception as exc:
            return {"ok": False, "status": "blocked", "reason": "polymarket_live_signing_not_ready", "detail": str(exc), "order_args": order_args}

    def dry_run_order(self, *, token_id: str, price: float, size: float, side: str = BUY, tick_size: str | None = None, neg_risk: bool | None = None) -> dict[str, Any]:
        signed = self.create_signed_order(token_id=token_id, price=price, size=size, side=side, tick_size=tick_size, neg_risk=neg_risk)
        signed["order_endpoint_called"] = False
        signed["sent"] = False
        return signed

    def post_order(self, signed_order: Any, order_type: str = "FOK", gate_context: dict[str, Any] | None = None) -> dict[str, Any]:
        if not _env_true("POLYLENS_POLYMARKET_LIVE_SENDS_ENABLED"):
            return {"ok": False, "status": "blocked", "reason": "polymarket_live_sends_disabled", "sent": False, "order_endpoint_called": False}
        gate = validate_first_live_send_gates(gate_context or {})
        if not gate["ok"]:
            return {"ok": False, "status": "blocked", "reason": "polymarket_first_live_gates_failed", "failed_gates": gate["failed_gates"], "sent": False, "order_endpoint_called": False}
        self._post_order_called = True
        client = self.build_client()
        sdk = _load_sdk()
        if sdk is None:
            return {"ok": False, "status": "blocked", "reason": "polymarket_live_signing_not_ready", "sent": False, "order_endpoint_called": False}
        _package, _client_cls, _creds_cls, _order_args_cls, order_type_cls, _options_cls = sdk
        sdk_order_type = getattr(order_type_cls, order_type, order_type)
        response = client.post_order(signed_order, sdk_order_type)
        return {"ok": True, "status": "submitted", "response": _redact(response), "sent": True, "order_endpoint_called": True}

    @staticmethod
    def order_args(*, token_id: str, price: float, size: float, side: str = BUY) -> dict[str, Any]:
        return {"token_id": str(token_id), "price": round(float(price), 4), "size": round(float(size), 8), "side": side}


class PolymarketLiveError(RuntimeError):
    pass


SHORT_CRYPTO_ASSETS = ("BTC", "ETH", "SOL")
SHORT_CRYPTO_WINDOWS = (5, 10, 15)


def discover_short_crypto_market(
    assets: tuple[str, ...] = SHORT_CRYPTO_ASSETS,
    windows: tuple[int, ...] = SHORT_CRYPTO_WINDOWS,
    limit: int = 500,
) -> dict[str, Any]:
    rejected: list[dict[str, Any]] = []
    candidates_checked: list[dict[str, Any]] = []
    clob_book_404_token_ids: list[str] = []
    seen_404: set[str] = set()

    for asset in assets:
        for window_minutes in windows:
            markets = _gamma_markets(asset=asset, window_minutes=window_minutes, limit=limit)
            for market in markets:
                parsed = _parse_market_tokens(market)
                slug = str(market.get("slug") or market.get("marketSlug") or market.get("id") or "")
                if not parsed:
                    rejected.append({"market_slug": slug, "reason": "token_ids_missing", "asset": asset, "window_minutes": window_minutes})
                    continue
                for outcome in parsed["outcomes"]:
                    if outcome["name"].upper() not in {"YES", "UP"}:
                        continue
                    token_id = outcome["token_id"]
                    probe = _probe_orderbook(token_id)
                    candidates_checked.append(
                        {
                            "asset": asset,
                            "window_minutes": window_minutes,
                            "market_slug": slug,
                            "token_id": token_id,
                            "outcome": outcome["name"],
                            "book_status": probe.get("status_code"),
                            "book_reason": probe.get("reason"),
                        }
                    )
                    if probe.get("status_code") == 404 and token_id not in seen_404:
                        seen_404.add(token_id)
                        clob_book_404_token_ids.append(token_id)
                    if not probe.get("ok"):
                        rejected.append(
                            {
                                "market_slug": slug,
                                "token_id": token_id,
                                "reason": probe.get("reason", "orderbook_fetch_failed"),
                                "detail": probe.get("detail"),
                                "asset": asset,
                                "window_minutes": window_minutes,
                            }
                        )
                        continue
                    book = probe["book"]
                    ask = best_ask(book)
                    if not ask:
                        rejected.append(
                            {
                                "market_slug": slug,
                                "token_id": token_id,
                                "reason": "no_executable_ask",
                                "asset": asset,
                                "window_minutes": window_minutes,
                            }
                        )
                        continue
                    return {
                        "ok": True,
                        "mode": "short_crypto",
                        "market": market,
                        "outcome": outcome,
                        "orderbook": book,
                        "best_ask": ask,
                        "rejected": rejected,
                        "candidates_checked": candidates_checked,
                        "clob_book_404_token_ids": clob_book_404_token_ids,
                        "not_short_crypto": False,
                    }

    return {
        "ok": False,
        "mode": "short_crypto",
        "reason": "no_short_crypto_clob_book_available",
        "rejected": rejected,
        "candidates_checked": candidates_checked,
        "clob_book_404_token_ids": clob_book_404_token_ids,
        "not_short_crypto": False,
    }


def discover_clob_connectivity_market(asset: str = "BTC", window_minutes: int = 5, limit: int = 500) -> dict[str, Any]:
    short = discover_short_crypto_market(limit=limit)
    if short.get("ok"):
        result = dict(short)
        result["mode"] = "clob_connectivity"
        result["not_short_crypto"] = False
        result["purpose"] = None
        return result
    rejected = list(short.get("rejected") or [])
    fallback = _gamma_price_markets(asset=asset, limit=limit)
    result = _discover_from_markets(fallback, rejected)
    result["mode"] = "clob_connectivity"
    result["candidates_checked"] = short.get("candidates_checked", [])
    result["clob_book_404_token_ids"] = short.get("clob_book_404_token_ids", [])
    result["short_crypto_discovery"] = {"ok": False, "reason": short.get("reason")}
    if result.get("ok"):
        result["not_short_crypto"] = True
        result["purpose"] = "clob_connectivity_only"
    else:
        result.setdefault("not_short_crypto", False)
    return result


def discover_crypto_market(asset: str = "BTC", window_minutes: int = 5, limit: int = 500) -> dict[str, Any]:
    return discover_clob_connectivity_market(asset=asset, window_minutes=window_minutes, limit=limit)


def _discover_from_markets(markets: list[dict[str, Any]], rejected: list[dict[str, Any]]) -> dict[str, Any]:
    for market in markets:
        parsed = _parse_market_tokens(market)
        if not parsed:
            rejected.append({"market_slug": market.get("slug"), "reason": "token_ids_missing"})
            continue
        for outcome in parsed["outcomes"]:
            if outcome["name"].upper() not in {"YES", "UP"}:
                continue
            probe = _probe_orderbook(outcome["token_id"])
            if not probe.get("ok"):
                rejected.append(
                    {
                        "market_slug": market.get("slug"),
                        "token_id": outcome["token_id"],
                        "reason": probe.get("reason", "orderbook_fetch_failed"),
                        "detail": probe.get("detail"),
                        "book_status": probe.get("status_code"),
                    }
                )
                continue
            book = probe["book"]
            ask = best_ask(book)
            if not ask:
                rejected.append({"market_slug": market.get("slug"), "token_id": outcome["token_id"], "reason": "no_executable_ask"})
                continue
            return {"ok": True, "market": market, "outcome": outcome, "orderbook": book, "best_ask": ask, "rejected": rejected}
    return {"ok": False, "reason": "no_executable_polymarket_crypto_candidate_found", "rejected": rejected}


def _probe_orderbook(token_id: str, host: str | None = None) -> dict[str, Any]:
    host = (host or os.environ.get("POLYMARKET_CLOB_HOST") or DEFAULT_CLOB_HOST).rstrip("/")
    url = f"{host}/book?{urlencode({'token_id': token_id})}"
    try:
        book = _get_json(url)
        if not isinstance(book, dict):
            return {"ok": False, "book": None, "status_code": None, "reason": "orderbook_fetch_failed", "detail": "invalid response"}
        return {"ok": True, "book": book, "status_code": 200}
    except HTTPError as exc:
        reason = "clob_book_not_found" if exc.code == 404 else "orderbook_fetch_failed"
        return {"ok": False, "book": None, "status_code": exc.code, "reason": reason, "detail": str(exc)}
    except Exception as exc:
        return {"ok": False, "book": None, "status_code": None, "reason": "orderbook_fetch_failed", "detail": str(exc)}


def get_orderbook(token_id: str, host: str | None = None) -> dict[str, Any]:
    probe = _probe_orderbook(token_id, host=host)
    if not probe.get("ok"):
        raise PolymarketLiveError(probe.get("reason", "orderbook_fetch_failed"))
    return probe["book"]


def best_ask(book: dict[str, Any]) -> dict[str, Any] | None:
    best = None
    for raw in book.get("asks") or []:
        try:
            price = float(raw.get("price") if isinstance(raw, dict) else raw[0])
            size = float(raw.get("size") if isinstance(raw, dict) else raw[1])
        except Exception:
            continue
        if price <= 0 or price >= 1 or size <= 0:
            continue
        candidate = {"price": price, "size": size, "raw": raw}
        if best is None or candidate["price"] < best["price"]:
            best = candidate
    return best


def first_live_duplicate_key(*, market_slug: str, token_id: str, run_id: str) -> str:
    raw = f"polymarket|{market_slug}|{token_id}|{run_id}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def validate_first_live_send_gates(context: dict[str, Any]) -> dict[str, Any]:
    failed: list[str] = []
    checks = {
        "POLYLENS_FIRST_LIVE_TEST": _env_true("POLYLENS_FIRST_LIVE_TEST"),
        "POLYLENS_LIVE_TRADING": _env_true("POLYLENS_LIVE_TRADING"),
        "POLYLENS_AUTONOMOUS_CRYPTO": _env_true("POLYLENS_AUTONOMOUS_CRYPTO"),
        "POLYLENS_CONFIRM_RISK_ACK": _env_true("POLYLENS_CONFIRM_RISK_ACK"),
        "POLYLENS_POLYMARKET_LIVE_SENDS_ENABLED": _env_true("POLYLENS_POLYMARKET_LIVE_SENDS_ENABLED"),
    }
    for name, ok in checks.items():
        if not ok:
            failed.append(f"missing_live_gate_{name.lower()}")
    if not context.get("run_id"):
        failed.append("missing_first_live_test_run_id")
    if float(context.get("max_exposure") or 999) > 1.0:
        failed.append("first_live_test_max_exposure_gt_1")
    if int(context.get("orders_count") or 0) != 1:
        failed.append("first_live_test_max_one_order")
    if context.get("duplicate"):
        failed.append("duplicate_trade_key")
    if not context.get("fresh_orderbook"):
        failed.append("stale_clob_orderbook")
    if not context.get("executable_ask"):
        failed.append("no_executable_ask")
    if not context.get("balance_allowance_ok"):
        failed.append("balance_allowance_not_ready")
    if context.get("not_short_crypto"):
        failed.append("not_short_crypto_market")
    return {"ok": not failed, "failed_gates": failed}


def _gamma_markets(asset: str, window_minutes: int, limit: int) -> list[dict[str, Any]]:
    params = {"active": "true", "closed": "false", "limit": min(int(limit), 200), "tag_slug": "crypto"}
    rows = _get_json(f"{DEFAULT_GAMMA_HOST}/events?{urlencode(params)}")
    events = rows if isinstance(rows, list) else rows.get("events", []) if isinstance(rows, dict) else []
    markets = []
    for event in events:
        for market in event.get("markets") or []:
            enriched = dict(market)
            enriched.setdefault("event_slug", event.get("slug"))
            enriched.setdefault("event_title", event.get("title"))
            markets.append(enriched)
    asset_terms = {"BTC": ("BTC", "BITCOIN"), "ETH": ("ETH", "ETHEREUM"), "SOL": ("SOL", "SOLANA")}
    terms = asset_terms.get(asset.upper(), (asset.upper(),))
    out = []
    for market in markets:
        text = " ".join(str(market.get(key) or "") for key in ("question", "title", "slug")).upper()
        if any(term in text for term in terms) and _looks_like_short_crypto(text, asset, window_minutes):
            out.append(market)
    return out


def _gamma_price_markets(asset: str, limit: int) -> list[dict[str, Any]]:
    params = {"active": "true", "closed": "false", "limit": min(int(limit), 200), "tag_slug": "crypto"}
    rows = _get_json(f"{DEFAULT_GAMMA_HOST}/events?{urlencode(params)}")
    events = rows if isinstance(rows, list) else rows.get("events", []) if isinstance(rows, dict) else []
    asset_terms = {"BTC": ("BTC", "BITCOIN"), "ETH": ("ETH", "ETHEREUM"), "SOL": ("SOL", "SOLANA")}
    terms = asset_terms.get(asset.upper(), (asset.upper(),))
    out = []
    for event in events:
        for market in event.get("markets") or []:
            enriched = dict(market)
            enriched.setdefault("event_slug", event.get("slug"))
            enriched.setdefault("event_title", event.get("title"))
            text = " ".join(str(enriched.get(key) or "") for key in ("question", "title", "slug")).upper()
            if any(term in text for term in terms) and _looks_like_crypto_price_market(text):
                out.append(enriched)
    return out


def _looks_like_short_crypto(text: str, asset: str, window_minutes: int) -> bool:
    text = text.upper()
    price_terms = ("PRICE", "UP OR DOWN", "UP-OR-DOWN", "UPDOWN", "ABOVE OR BELOW", "HIGHER OR LOWER")
    if not any(term in text for term in price_terms):
        return False
    blocked = ("MICROSTRATEGY", "MSTR", "PURCHASE", "ETF", "RESERVE", "TREASURY")
    if any(term in text for term in blocked):
        return False
    window_terms = (f"{window_minutes}M", f"{window_minutes} M", f"{window_minutes}-MIN", f"{window_minutes} MIN", f"{window_minutes}MIN")
    slug_window_terms = (f"{window_minutes}m", f"{window_minutes}-min")
    return any(term in text for term in window_terms) or any(term.upper() in text for term in slug_window_terms)


def _looks_like_crypto_price_market(text: str) -> bool:
    if not any(term in text for term in ("PRICE", "REACH", "HIT", "ALL TIME HIGH")):
        return False
    blocked = ("MICROSTRATEGY", "MSTR", "PURCHASE", "ETF", "RESERVE", "TREASURY", "COUNTRY", "COMPANY")
    return not any(term in text for term in blocked)


def _parse_market_tokens(market: dict[str, Any]) -> dict[str, Any] | None:
    outcomes = _json_list(market.get("outcomes"))
    token_ids = _json_list(market.get("clobTokenIds") or market.get("clob_token_ids"))
    if not outcomes or not token_ids or len(outcomes) != len(token_ids):
        return None
    return {"outcomes": [{"name": str(name), "token_id": str(token_id)} for name, token_id in zip(outcomes, token_ids)]}


def _json_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else []
        except Exception:
            return []
    return []


def _get_json(url: str) -> Any:
    request = Request(url, headers={"User-Agent": "polylens/0.1", "Accept": "application/json"})
    with urlopen(request, timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))


def _load_sdk() -> tuple[str, Any, Any, Any, Any, Any] | None:
    try:
        from py_clob_client_v2 import ApiCreds, ClobClient, OrderArgs, OrderType, PartialCreateOrderOptions

        return ("py_clob_client_v2", ClobClient, ApiCreds, OrderArgs, OrderType, PartialCreateOrderOptions)
    except Exception:
        pass
    try:
        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import ApiCreds, OrderArgs, OrderType

        return ("py_clob_client", ClobClient, ApiCreds, OrderArgs, OrderType, None)
    except Exception:
        return None


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: ("<redacted>" if any(secret in key.lower() for secret in ("key", "secret", "passphrase", "signature", "private")) else _redact(val)) for key, val in value.items()}
    if hasattr(value, "dict"):
        return _redact(value.dict())
    if hasattr(value, "__dict__"):
        return _redact(value.__dict__)
    return value


def _clean(value: str | None) -> str | None:
    value = (value or "").strip()
    return value or None


def _int_or_none(value: str | None) -> int | None:
    try:
        return int(value) if value not in {None, ""} else None
    except Exception:
        return None


def _env_true(name: str) -> bool:
    value = os.environ.get(name)
    return bool(value and value.lower() in {"1", "true", "yes", "on"})
