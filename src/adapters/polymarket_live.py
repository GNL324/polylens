from __future__ import annotations

import hashlib
import re
import json
import os
import time
from datetime import datetime, timezone
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
        empty = {
            "auth_valid": False,
            "balance_allowance_attempts": [],
            "first_successful_asset_type": None,
            "balance_summary": None,
        }
        try:
            client = self.build_client()
        except PolymarketLiveError as exc:
            return {"ok": False, "status": "not_ready", "reason": str(exc), **empty}
        sdk = _load_sdk()
        method = getattr(client, "get_balance_allowance", None)
        if not callable(method):
            method = getattr(client, "get_balance_and_allowance", None)
        if not callable(method):
            return {
                "ok": False,
                "status": "not_ready",
                "reason": "balance_allowance_method_not_available",
                **empty,
            }

        attempts: list[dict[str, Any]] = []
        first_successful_asset_type: str | None = None
        balance_summary: dict[str, Any] | None = None

        for label, asset_type_value in _balance_allowance_asset_type_candidates(sdk):
            try:
                params = _build_balance_allowance_params(sdk, asset_type_value)
                raw = method(params) if params is not None else method()
                attempts.append(
                    {
                        "asset_type": label,
                        "ok": True,
                        "status": "ok",
                        "status_code": 200,
                    }
                )
                if first_successful_asset_type is None:
                    first_successful_asset_type = label
                    balance_summary = _summarize_balance_response(raw)
                break
            except Exception as exc:
                status_code, error_msg = _parse_poly_api_error(exc)
                attempts.append(
                    {
                        "asset_type": label,
                        "ok": False,
                        "status": "error",
                        "status_code": status_code,
                        "error": _safe_balance_error_message(error_msg),
                    }
                )
                if status_code == 401:
                    break

        auth_valid = derive_auth_valid_from_attempts(attempts)
        ok = first_successful_asset_type is not None
        result: dict[str, Any] = {
            "ok": ok,
            "status": "ok" if ok else "not_ready",
            "auth_valid": auth_valid,
            "balance_allowance_attempts": attempts,
            "first_successful_asset_type": first_successful_asset_type,
            "balance_summary": balance_summary,
        }
        if not ok:
            result["reason"] = _balance_allowance_failure_reason(attempts, auth_valid)
        return result

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

    def try_init_client(self) -> dict[str, Any]:
        try:
            client = self.build_client()
            return {"ok": True, "client_type": type(client).__name__}
        except PolymarketLiveError as exc:
            return {"ok": False, "reason": str(exc)}
        except Exception as exc:
            return {"ok": False, "reason": "clob_client_init_failed", "detail": str(exc)}

    @staticmethod
    def order_args(*, token_id: str, price: float, size: float, side: str = BUY) -> dict[str, Any]:
        return {"token_id": str(token_id), "price": round(float(price), 4), "size": round(float(size), 8), "side": side}


class PolymarketLiveError(RuntimeError):
    pass


SHORT_CRYPTO_ASSETS = ("BTC", "ETH", "SOL")
SHORT_CRYPTO_WINDOWS = (5, 10, 15)

SHORT_CRYPTO_SERIES_BY_ASSET: dict[str, dict[int, str]] = {
    "BTC": {5: "btc-up-or-down-5m", 15: "btc-up-or-down-15m"},
    "ETH": {5: "eth-up-or-down-5m", 15: "eth-up-or-down-15m"},
    "SOL": {5: "sol-up-or-down-5m", 15: "sol-up-or-down-15m"},
}


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
            markets: list[dict[str, Any]] = []
            seen_slugs: set[str] = set()

            def _append_market(candidate: dict[str, Any]) -> None:
                slug_key = str(candidate.get("slug") or candidate.get("marketSlug") or candidate.get("id") or "")
                if slug_key and slug_key in seen_slugs:
                    return
                if slug_key:
                    seen_slugs.add(slug_key)
                markets.append(candidate)

            series_slug = SHORT_CRYPTO_SERIES_BY_ASSET.get(asset.upper(), {}).get(window_minutes)
            if series_slug:
                for series_market in series_live_event_markets(series_slug):
                    _append_market(series_market)
            for legacy_market in _gamma_markets(asset=asset, window_minutes=window_minutes, limit=limit):
                _append_market(legacy_market)
            for market in markets:
                parsed = _parse_market_tokens(market)
                slug = str(market.get("slug") or market.get("marketSlug") or market.get("id") or "")
                condition_id = _market_condition_id(market)
                clob_token_ids = _market_clob_token_ids(market)
                if not parsed:
                    rejected.append(
                        {
                            "slug": slug,
                            "condition_id": condition_id,
                            "clobTokenIds": clob_token_ids,
                            "reason": "token_ids_missing",
                            "asset": asset,
                            "window_minutes": window_minutes,
                        }
                    )
                    continue
                eligible, skip_reason = _short_crypto_market_eligible(market)
                if not eligible:
                    rejected.append(
                        {
                            "slug": slug,
                            "market_slug": slug,
                            "condition_id": condition_id,
                            "clobTokenIds": clob_token_ids,
                            "reason": skip_reason,
                            "asset": asset,
                            "window_minutes": window_minutes,
                            "enable_order_book": _market_enable_order_book(market),
                            "accepting_orders": _market_accepting_orders(market),
                            "seconds_to_close": _market_seconds_to_close(market),
                        }
                    )
                    continue
                for outcome in parsed["outcomes"]:
                    if outcome["name"].upper() not in {"YES", "UP"}:
                        continue
                    token_id = outcome["token_id"]
                    probe = _probe_orderbook_with_variants(token_id)
                    candidate = {
                        "asset": asset,
                        "window_minutes": window_minutes,
                        "slug": slug,
                        "market_slug": slug,
                        "condition_id": condition_id,
                        "clobTokenIds": clob_token_ids,
                        "token_id": token_id,
                        "token_id_tried": [item["token_id_tried"] for item in probe.get("attempts", [])],
                        "outcome": outcome["name"],
                        "book_status": probe.get("status_code"),
                        "book_reason": probe.get("reason"),
                        "error": probe.get("error") or probe.get("detail"),
                        "probe_attempts": probe.get("attempts", []),
                    }
                    candidates_checked.append(candidate)
                    if probe.get("status_code") == 404 and token_id not in seen_404:
                        seen_404.add(token_id)
                        clob_book_404_token_ids.append(token_id)
                    if not probe.get("ok"):
                        rejected.append(
                            {
                                "slug": slug,
                                "market_slug": slug,
                                "condition_id": condition_id,
                                "clobTokenIds": clob_token_ids,
                                "token_id": token_id,
                                "outcome": outcome["name"],
                                "reason": probe.get("reason", "orderbook_fetch_failed"),
                                "detail": probe.get("detail"),
                                "error": probe.get("error"),
                                "asset": asset,
                                "window_minutes": window_minutes,
                                "book_status": probe.get("status_code"),
                                "token_id_tried": candidate["token_id_tried"],
                            }
                        )
                        continue
                    book = probe["book"]
                    ask = best_ask(book)
                    if not ask:
                        rejected.append(
                            {
                                "slug": slug,
                                "market_slug": slug,
                                "condition_id": condition_id,
                                "token_id": token_id,
                                "outcome": outcome["name"],
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

    failure: dict[str, Any] = {
        "ok": False,
        "mode": "short_crypto",
        "reason": "no_short_crypto_clob_book_available",
        "rejected": rejected,
        "candidates_checked": candidates_checked,
        "clob_book_404_token_ids": clob_book_404_token_ids,
        "not_short_crypto": False,
    }
    diagnostics = _short_crypto_clob_diagnostics(candidates_checked)
    if diagnostics:
        failure["diagnostics"] = diagnostics
    return failure


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


def _token_id_probe_variants(token_id: Any) -> list[str]:
    variants: list[str] = []
    for value in (token_id, str(token_id)):
        candidate = str(value).strip()
        if candidate and candidate not in variants:
            variants.append(candidate)
    return variants


def _market_condition_id(market: dict[str, Any]) -> str | None:
    value = market.get("conditionId") or market.get("condition_id")
    return str(value) if value not in {None, ""} else None


def _market_clob_token_ids(market: dict[str, Any]) -> list[Any]:
    return _json_list(market.get("clobTokenIds") or market.get("clob_token_ids"))


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


def _probe_orderbook_with_variants(token_id: Any, host: str | None = None) -> dict[str, Any]:
    attempts: list[dict[str, Any]] = []
    for variant in _token_id_probe_variants(token_id):
        probe = _probe_orderbook(variant, host=host)
        attempt = {
            "token_id_tried": variant,
            "book_status": probe.get("status_code"),
            "ok": probe.get("ok"),
            "reason": probe.get("reason"),
            "error": probe.get("detail"),
        }
        attempts.append(attempt)
        if probe.get("ok"):
            return {
                "ok": True,
                "book": probe["book"],
                "status_code": probe.get("status_code") or 200,
                "token_id_used": variant,
                "attempts": attempts,
                "reason": None,
                "detail": None,
                "error": None,
            }
    last = attempts[-1] if attempts else {}
    return {
        "ok": False,
        "book": None,
        "status_code": last.get("book_status"),
        "token_id_used": None,
        "attempts": attempts,
        "reason": last.get("reason") or "orderbook_fetch_failed",
        "detail": last.get("error"),
        "error": last.get("error"),
    }


def _short_crypto_clob_diagnostics(candidates_checked: list[dict[str, Any]]) -> dict[str, Any]:
    five_m = [item for item in candidates_checked if item.get("window_minutes") == 5]
    if not five_m:
        return {}
    if not all(item.get("book_status") == 404 for item in five_m):
        return {}
    return {
        "all_5m_books_404": True,
        "five_m_candidate_count": len(five_m),
        "unique_slugs": sorted({str(item.get("slug") or "") for item in five_m if item.get("slug")}),
        "unique_condition_ids": sorted({str(item.get("condition_id") or "") for item in five_m if item.get("condition_id")}),
        "clob_book_404_token_ids": sorted({str(item.get("token_id") or "") for item in five_m if item.get("token_id")}),
        "outcomes": sorted({str(item.get("outcome") or "") for item in five_m if item.get("outcome")}),
        "probe_samples": five_m[:5],
    }


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
        "POLYLENS_POLYMARKET_FIRST_LIVE_TEST": _env_true("POLYLENS_POLYMARKET_FIRST_LIVE_TEST"),
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


def probe_sdk() -> dict[str, Any]:
    imports: dict[str, dict[str, Any]] = {}
    for package in ("py_clob_client_v2", "py_clob_client"):
        try:
            if package == "py_clob_client_v2":
                from py_clob_client_v2 import ApiCreds, ClobClient, OrderArgs, OrderType, PartialCreateOrderOptions  # noqa: F401
            else:
                from py_clob_client.client import ClobClient  # noqa: F401
                from py_clob_client.clob_types import ApiCreds, OrderArgs, OrderType  # noqa: F401
            imports[package] = {"ok": True, "error": None}
        except Exception as exc:
            imports[package] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    loaded = _load_sdk()
    return {
        "sdk_available": loaded is not None,
        "sdk_package": loaded[0] if loaded else None,
        "imports": imports,
    }


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



def _balance_allowance_asset_type_candidates(sdk: tuple[str, Any, Any, Any, Any, Any] | None) -> list[tuple[str, Any]]:
    candidates: list[tuple[str, Any]] = []
    seen: set[str] = set()

    def add(label: str, value: Any) -> None:
        if label in seen:
            return
        seen.add(label)
        candidates.append((label, value))

    if sdk and sdk[0] == "py_clob_client_v2":
        try:
            from py_clob_client_v2.clob_types import AssetType

            add("COLLATERAL", AssetType.COLLATERAL)
            add("CONDITIONAL", AssetType.CONDITIONAL)
        except Exception:
            pass

    for label in ("COLLATERAL", "CONDITIONAL", "collateral", "conditional", "usdc", "token"):
        add(label, label)
    add("none", None)
    return candidates


def _build_balance_allowance_params(sdk: tuple[str, Any, Any, Any, Any, Any] | None, asset_type_value: Any) -> Any | None:
    if asset_type_value is None:
        return None
    if sdk and sdk[0] == "py_clob_client_v2":
        from py_clob_client_v2.clob_types import BalanceAllowanceParams

        return BalanceAllowanceParams(asset_type=asset_type_value)
    if sdk and sdk[0] == "py_clob_client":
        try:
            from py_clob_client.clob_types import BalanceAllowanceParams

            return BalanceAllowanceParams(asset_type=asset_type_value)
        except Exception:
            pass
    return {"asset_type": asset_type_value}


def _parse_poly_api_error(exc: Exception) -> tuple[int | None, Any]:
    status_code = getattr(exc, "status_code", None)
    error_msg = getattr(exc, "error_msg", None)
    if error_msg is None:
        error_msg = str(exc)
    return status_code, error_msg


def derive_auth_valid_from_attempts(attempts: list[dict[str, Any]]) -> bool:
    if not attempts:
        return False
    if any(attempt.get("status_code") == 401 for attempt in attempts):
        return False
    return any(attempt.get("status_code") in {200, 400} for attempt in attempts)


def _balance_allowance_failure_reason(attempts: list[dict[str, Any]], auth_valid: bool) -> str:
    if auth_valid:
        return "balance_allowance_request_malformed"
    if any(attempt.get("status_code") == 401 for attempt in attempts):
        return "balance_allowance_unauthorized"
    return "balance_allowance_check_failed"


def _safe_balance_error_message(error_msg: Any) -> Any:
    if isinstance(error_msg, dict):
        return {
            key: value
            for key, value in error_msg.items()
            if str(key).lower() not in {"token_id", "asset_id"}
        }
    return _redact(error_msg)


def _summarize_balance_response(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {"present": bool(raw)}
    summary: dict[str, Any] = {}
    for key in ("balance", "allowance", "available", "allowances", "amount", "asset_type"):
        if key in raw:
            summary[key] = raw[key]
    redacted = _redact(summary)
    return redacted if isinstance(redacted, dict) else {"present": True}


KNOWN_GOOD_CLOB_MARKET_SLUG = "will-bitcoin-hit-150k-by-june-30-2026"


def _market_enable_order_book(market: dict[str, Any]) -> bool | None:
    for key in ("enableOrderBook", "enable_order_book"):
        if key in market and market[key] is not None:
            return bool(market[key])
    return None


def _market_accepting_orders(market: dict[str, Any]) -> bool | None:
    for key in ("acceptingOrders", "accepting_orders"):
        if key in market and market[key] is not None:
            return bool(market[key])
    return None


def _market_end_datetime(market: dict[str, Any]) -> datetime | None:
    for key in ("endDate", "end_date", "endDateIso"):
        raw = market.get(key)
        if not raw:
            continue
        try:
            return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except Exception:
            continue
    return None


def _market_seconds_to_close(market: dict[str, Any], *, now: datetime | None = None) -> int | None:
    end = _market_end_datetime(market)
    if end is None:
        return None
    now = now or datetime.now(timezone.utc)
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    return int((end - now).total_seconds())


def _short_crypto_market_eligible(market: dict[str, Any], *, now: datetime | None = None) -> tuple[bool, str | None]:
    enabled = _market_enable_order_book(market)
    if enabled is False:
        return False, "gamma_market_not_clob_enabled"
    seconds = _market_seconds_to_close(market, now=now)
    if seconds is not None and seconds <= 0:
        return False, "market_window_expired"
    return True, None


def collect_market_token_fields(market: dict[str, Any]) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    for key, value in market.items():
        lowered = key.lower()
        if "token" in lowered or "clob" in lowered:
            fields[key] = value
    tokens = market.get("tokens")
    if isinstance(tokens, list):
        fields["tokens"] = [
            {
                "token_id": item.get("token_id") or item.get("tokenId"),
                "outcome": item.get("outcome"),
            }
            for item in tokens
            if isinstance(item, dict)
        ]
    return fields


def discover_candidate_token_ids(market: dict[str, Any]) -> list[str]:
    token_ids: list[str] = []
    parsed = _parse_market_tokens(market)
    if parsed:
        for outcome in parsed["outcomes"]:
            token_ids.extend(_token_id_probe_variants(outcome["token_id"]))
    for key in ("clobTokenIds", "clob_token_ids"):
        for value in _json_list(market.get(key)):
            token_ids.extend(_token_id_probe_variants(value))
    tokens = market.get("tokens")
    if isinstance(tokens, list):
        for item in tokens:
            if isinstance(item, dict):
                token_ids.extend(_token_id_probe_variants(item.get("token_id") or item.get("tokenId")))
    deduped: list[str] = []
    for token_id in token_ids:
        if token_id and token_id not in deduped:
            deduped.append(token_id)
    return deduped


def _probe_clob_endpoint(path: str, token_id: str, host: str | None = None) -> dict[str, Any]:
    host = (host or os.environ.get("POLYMARKET_CLOB_HOST") or DEFAULT_CLOB_HOST).rstrip("/")
    url = f"{host}/{path}?{urlencode({'token_id': token_id})}"
    try:
        payload = _get_json(url)
        return {"ok": True, "status_code": 200, "data_present": payload is not None}
    except HTTPError as exc:
        detail = str(exc)
        try:
            body = exc.read().decode("utf-8")
        except Exception:
            body = detail
        return {"ok": False, "status_code": exc.code, "error": body[:200]}
    except Exception as exc:
        return {"ok": False, "status_code": None, "error": str(exc)[:200]}


def probe_clob_token_endpoints(token_id: str, host: str | None = None) -> dict[str, Any]:
    book_probe = _probe_orderbook(token_id, host=host)
    book = book_probe.get("book") if book_probe.get("ok") else None
    return {
        "token_id": str(token_id),
        "book": {
            "ok": bool(book_probe.get("ok")),
            "status_code": book_probe.get("status_code"),
            "reason": book_probe.get("reason"),
            "executable_book": bool(book and best_ask(book)),
        },
        "price": _probe_clob_endpoint("price", token_id, host=host),
        "midpoint": _probe_clob_endpoint("midpoint", token_id, host=host),
        "tick_size": _probe_clob_endpoint("tick-size", token_id, host=host),
    }


def gamma_market_by_slug(slug: str) -> dict[str, Any] | None:
    rows = _get_json(f"{DEFAULT_GAMMA_HOST}/markets?{urlencode({'slug': slug})}")
    if isinstance(rows, list) and rows:
        return rows[0]
    return None


def build_short_crypto_candidate_audit(market: dict[str, Any], *, asset: str, window_minutes: int) -> dict[str, Any]:
    slug = str(market.get("slug") or market.get("marketSlug") or market.get("id") or "")
    eligible, eligibility_reason = _short_crypto_market_eligible(market)
    token_ids = discover_candidate_token_ids(market)
    probes = [probe_clob_token_endpoints(token_id) for token_id in token_ids]
    primary_probe = probes[0] if probes else None
    return {
        "asset": asset,
        "window_minutes": window_minutes,
        "slug": slug,
        "id": market.get("id"),
        "condition_id": _market_condition_id(market),
        "question": market.get("question"),
        "active": market.get("active"),
        "closed": market.get("closed"),
        "archived": market.get("archived"),
        "accepting_orders": _market_accepting_orders(market),
        "enable_order_book": _market_enable_order_book(market),
        "clobTokenIds_raw": market.get("clobTokenIds") or market.get("clob_token_ids"),
        "tokens": market.get("tokens"),
        "outcomes": market.get("outcomes"),
        "outcomePrices": market.get("outcomePrices") or market.get("outcome_prices"),
        "market_maker_address": market.get("marketMakerAddress") or market.get("market_maker_address"),
        "endDate": market.get("endDate") or market.get("end_date"),
        "end_date": market.get("endDate") or market.get("end_date"),
        "seconds_to_close": _market_seconds_to_close(market),
        "token_id_fields": collect_market_token_fields(market),
        "token_ids_discovered": token_ids,
        "token_probes": probes,
        "book_status": (primary_probe or {}).get("book", {}).get("status_code"),
        "executable_book": any(item.get("book", {}).get("executable_book") for item in probes),
        "eligible_for_probe": eligible,
        "eligibility_reason": eligibility_reason,
    }


def compare_short_crypto_vs_known_good(short_candidate: dict[str, Any], known_good: dict[str, Any]) -> dict[str, Any]:
    fields = (
        "enable_order_book",
        "accepting_orders",
        "active",
        "closed",
        "archived",
        "seconds_to_close",
        "executable_book",
        "book_status",
    )
    diffs: dict[str, Any] = {}
    for field in fields:
        short_val = short_candidate.get(field)
        good_val = known_good.get(field)
        if short_val != good_val:
            diffs[field] = {"short_crypto": short_val, "known_good": good_val}
    return {
        "short_crypto_slug": short_candidate.get("slug"),
        "known_good_slug": known_good.get("slug"),
        "field_diffs": diffs,
        "short_clobTokenIds_shape": type(short_candidate.get("clobTokenIds_raw")).__name__,
        "known_good_clobTokenIds_shape": type(known_good.get("clobTokenIds_raw")).__name__,
    }


def diagnose_short_crypto_clob_issue(candidates: list[dict[str, Any]], known_good: dict[str, Any]) -> str:
    if not candidates:
        return "unknown"
    if known_good.get("executable_book") and all(c.get("enable_order_book") is True for c in candidates):
        alt_ids = []
        for candidate in candidates:
            fields = candidate.get("token_id_fields") or {}
            tokens = fields.get("tokens") or []
            alt_ids.extend(str(item.get("token_id")) for item in tokens if item.get("token_id"))
        if alt_ids and all(not c.get("executable_book") for c in candidates):
            return "token_field_bug"
    if all(c.get("enable_order_book") is False for c in candidates):
        return "gamma_market_not_clob_enabled"
    if any(c.get("enable_order_book") is True for c in candidates) and all(not c.get("executable_book") for c in candidates):
        return "clob_registration_delay"
    if all(not c.get("executable_book") for c in candidates):
        return "gamma_market_not_clob_enabled"
    return "unknown"


def enumerate_short_crypto_discovery_audit(
    assets: tuple[str, ...] = SHORT_CRYPTO_ASSETS,
    windows: tuple[int, ...] = SHORT_CRYPTO_WINDOWS,
    limit: int = 500,
    known_good_slug: str = KNOWN_GOOD_CLOB_MARKET_SLUG,
) -> dict[str, Any]:
    candidates: list[dict[str, Any]] = []
    for asset in assets:
        for window_minutes in windows:
            for market in _gamma_markets(asset=asset, window_minutes=window_minutes, limit=limit):
                candidates.append(build_short_crypto_candidate_audit(market, asset=asset, window_minutes=window_minutes))
    known_market = gamma_market_by_slug(known_good_slug)
    known_good = (
        build_short_crypto_candidate_audit(known_market, asset="BTC", window_minutes=0)
        if known_market
        else {"slug": known_good_slug, "executable_book": False, "error": "known_good_market_not_found"}
    )
    sample = candidates[0] if candidates else {}
    comparison = compare_short_crypto_vs_known_good(sample, known_good) if sample else {}
    diagnosis = diagnose_short_crypto_clob_issue(candidates, known_good)
    return {
        "status": "ok",
        "candidate_count": len(candidates),
        "candidates": candidates,
        "known_good_market": known_good,
        "sample_comparison": comparison,
        "diagnosis": diagnosis,
        "notes": {
            "price_midpoint_not_book": "price/midpoint success does not count as executable book",
            "discovery_filters_added": ["gamma_market_not_clob_enabled", "market_window_expired"],
        },
    }


CRYPTO_DISCOVERY_KEYWORDS = (
    "BTC",
    "BITCOIN",
    "ETH",
    "ETHEREUM",
    "SOL",
    "SOLANA",
    "CRYPTO",
)


def _market_text_blob(market: dict[str, Any]) -> str:
    parts = [
        market.get("question"),
        market.get("title"),
        market.get("slug"),
        market.get("marketSlug"),
        market.get("event_title"),
        market.get("event_slug"),
    ]
    tags = market.get("tags")
    if isinstance(tags, list):
        parts.extend(str(tag) for tag in tags)
    elif isinstance(tags, str):
        parts.append(tags)
    categories = market.get("categories")
    if isinstance(categories, list):
        parts.extend(str(item) for item in categories)
    return " ".join(str(part or "") for part in parts).upper()


def _crypto_keyword_match(blob: str, keyword: str) -> bool:
    if keyword in {"BTC", "ETH", "SOL"}:
        return re.search(rf"(?<![A-Z0-9]){keyword}(?![A-Z0-9])", blob) is not None
    return keyword in blob


def market_looks_crypto_related(market: dict[str, Any]) -> bool:
    blob = _market_text_blob(market)
    return any(_crypto_keyword_match(blob, keyword) for keyword in CRYPTO_DISCOVERY_KEYWORDS)


def gamma_market_is_active_unexpired(
    market: dict[str, Any],
    *,
    now: datetime | None = None,
    require_not_archived: bool = True,
) -> bool:
    if market.get("closed") is True:
        return False
    if require_not_archived and market.get("archived") is True:
        return False
    if market.get("active") is False:
        return False
    seconds = _market_seconds_to_close(market, now=now)
    if seconds is not None and seconds <= 0:
        return False
    return True


def fetch_gamma_markets_page(*, limit: int = 100, offset: int = 0, extra_params: dict[str, str] | None = None) -> list[dict[str, Any]]:
    params: dict[str, Any] = {"active": "true", "closed": "false", "limit": min(int(limit), 100), "offset": int(offset)}
    if extra_params:
        params.update(extra_params)
    rows = _get_json(f"{DEFAULT_GAMMA_HOST}/markets?{urlencode(params)}")
    return rows if isinstance(rows, list) else []


def fetch_gamma_event_markets(*, tag_slug: str = "crypto", limit: int = 200) -> list[dict[str, Any]]:
    params = {"active": "true", "closed": "false", "limit": min(int(limit), 200), "tag_slug": tag_slug}
    rows = _get_json(f"{DEFAULT_GAMMA_HOST}/events?{urlencode(params)}")
    events = rows if isinstance(rows, list) else rows.get("events", []) if isinstance(rows, dict) else []
    markets: list[dict[str, Any]] = []
    for event in events:
        for market in event.get("markets") or []:
            enriched = dict(market)
            enriched["event_slug"] = event.get("slug")
            enriched["event_title"] = event.get("title")
            enriched["event_tags"] = event.get("tags")
            enriched["event_categories"] = event.get("categories")
            markets.append(enriched)
    return markets


def collect_gamma_tradable_crypto_candidates(
    *,
    page_limit: int = 100,
    max_pages: int = 10,
    include_events: bool = True,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    now = now or datetime.now(timezone.utc)
    seen: set[str] = set()
    collected: list[dict[str, Any]] = []

    def add_market(market: dict[str, Any]) -> None:
        if not market_looks_crypto_related(market):
            return
        if not gamma_market_is_active_unexpired(market, now=now):
            return
        slug = str(market.get("slug") or market.get("marketSlug") or market.get("id") or "")
        key = str(market.get("id") or slug)
        if not key or key in seen:
            return
        seen.add(key)
        collected.append(market)

    for page in range(max_pages):
        offset = page * page_limit
        rows = fetch_gamma_markets_page(limit=page_limit, offset=offset)
        if not rows:
            break
        for market in rows:
            add_market(market)
        if len(rows) < page_limit:
            break

    for page in range(max_pages):
        offset = page * page_limit
        rows = fetch_gamma_markets_page(limit=page_limit, offset=offset, extra_params={"enableOrderBook": "true"})
        if not rows:
            break
        for market in rows:
            add_market(market)
        if len(rows) < page_limit:
            break

    if include_events:
        for market in fetch_gamma_event_markets():
            add_market(market)

    for asset_map in SHORT_CRYPTO_SERIES_BY_ASSET.values():
        for series_slug in asset_map.values():
            for market in series_live_event_markets(series_slug):
                add_market(market)

    return collected


def guess_tradable_crypto_asset(market: dict[str, Any]) -> str:
    blob = _market_text_blob(market)
    if "BITCOIN" in blob or _crypto_keyword_match(blob, "BTC"):
        return "BTC"
    if "ETHEREUM" in blob or _crypto_keyword_match(blob, "ETH"):
        return "ETH"
    if "SOLANA" in blob or _crypto_keyword_match(blob, "SOL"):
        return "SOL"
    return "other"


def guess_market_window(market: dict[str, Any], *, seconds_to_close: int | None = None) -> str:
    blob = _market_text_blob(market).lower()
    slug = str(market.get("slug") or "").lower()
    combined = f"{blob} {slug}"
    if any(term in combined for term in ("5m", "5 m", "5-min", "5 min", "5minute", "5-minute")):
        return "5m"
    if any(term in combined for term in ("10m", "10 m", "10-min", "10 min", "10minute", "10-minute")):
        return "10m"
    if any(term in combined for term in ("15m", "15 m", "15-min", "15 min", "15minute", "15-minute")):
        return "15m"
    if any(term in combined for term in ("1h", "1 h", "hourly", "1-hour", "1 hour")):
        return "hour"
    if seconds_to_close is not None:
        if seconds_to_close <= 6 * 3600:
            return "hour"
        if seconds_to_close <= 48 * 3600:
            return "day"
        if seconds_to_close <= 35 * 86400:
            return "month"
    if any(term in combined for term in ("2025", "2026", "2027", "2028", "2029", "2030", "BY ", "BEFORE", "HIT", "REACH")):
        return "long_dated"
    return "unknown"


def is_short_crypto_market_candidate(market: dict[str, Any], *, asset: str, window_guess: str) -> bool:
    if asset not in {"BTC", "ETH", "SOL"}:
        return False
    if window_guess not in {"5m", "10m", "15m"}:
        return False
    blob = _market_text_blob(market)
    price_terms = ("PRICE", "UP OR DOWN", "UP/DOWN", "UPDOWN", "UP-DOWN", "ABOVE OR BELOW", "HIGHER OR LOWER")
    return any(term in blob for term in price_terms)


def probe_clob_token_tradable(token_id: str, host: str | None = None) -> dict[str, Any]:
    book_probe = _probe_orderbook(token_id, host=host)
    book = book_probe.get("book") if book_probe.get("ok") else None
    return {
        "token_id": str(token_id),
        "book": {
            "ok": bool(book_probe.get("ok")),
            "status_code": book_probe.get("status_code"),
            "executable_book": bool(book and best_ask(book)),
        },
        "midpoint": _probe_clob_endpoint("midpoint", token_id, host=host),
        "tick_size": _probe_clob_endpoint("tick-size", token_id, host=host),
    }


def build_tradable_crypto_market_record(market: dict[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    slug = str(market.get("slug") or market.get("marketSlug") or market.get("id") or "")
    seconds_to_close = _market_seconds_to_close(market, now=now)
    asset = guess_tradable_crypto_asset(market)
    window_guess = guess_market_window(market, seconds_to_close=seconds_to_close)
    enable_order_book = _market_enable_order_book(market)
    accepting_orders = _market_accepting_orders(market)
    token_ids = discover_candidate_token_ids(market)
    if enable_order_book is True and token_ids:
        token_probes = [probe_clob_token_tradable(token_ids[0])]
    else:
        token_probes = []
    has_executable_book = any(item.get("book", {}).get("executable_book") for item in token_probes)
    tradable_clob = bool(enable_order_book is True and has_executable_book and (seconds_to_close is None or seconds_to_close > 0))
    short_crypto = is_short_crypto_market_candidate(market, asset=asset, window_guess=window_guess)
    return {
        "slug": slug,
        "question": market.get("question"),
        "condition_id": _market_condition_id(market),
        "clobTokenIds": market.get("clobTokenIds") or market.get("clob_token_ids"),
        "outcomes": market.get("outcomes"),
        "enable_order_book": enable_order_book,
        "accepting_orders": accepting_orders,
        "active": market.get("active"),
        "closed": market.get("closed"),
        "archived": market.get("archived"),
        "endDate": market.get("endDate") or market.get("end_date"),
        "seconds_to_close": seconds_to_close,
        "event_slug": market.get("event_slug"),
        "event_title": market.get("event_title"),
        "tags": market.get("tags") or market.get("event_tags"),
        "categories": market.get("categories") or market.get("event_categories"),
        "token_ids": token_ids,
        "token_probes": token_probes,
        "tradable_clob": tradable_clob,
        "has_executable_book": has_executable_book,
        "asset": asset,
        "window_guess": window_guess,
        "is_short_crypto_candidate": short_crypto,
        "liquidity_clob": market.get("liquidityClob") or market.get("liquidity_clob"),
        "volume_24hr_clob": market.get("volume24hrClob") or market.get("volume24hr_clob"),
    }


def diagnose_tradable_crypto_discovery(summary: dict[str, Any]) -> str:
    if summary.get("short_crypto_executable_books", 0) > 0:
        return "short_crypto_clob_available"
    if summary.get("executable_books", 0) > 0:
        return "crypto_clob_available_no_short_windows"
    if summary.get("clob_enabled_candidates", 0) > 0:
        return "clob_enabled_but_no_executable_books"
    if summary.get("total_gamma_candidates", 0) > 0:
        return "crypto_candidates_found_none_tradable"
    return "no_crypto_candidates_found"


def build_tradable_crypto_discovery(
    *,
    page_limit: int = 100,
    max_pages: int = 10,
    include_events: bool = True,
    top_n: int = 20,
    now: datetime | None = None,
) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    raw_markets = collect_gamma_tradable_crypto_candidates(
        page_limit=page_limit,
        max_pages=max_pages,
        include_events=include_events,
        now=now,
    )
    records = [build_tradable_crypto_market_record(market, now=now) for market in raw_markets]
    clob_enabled = [item for item in records if item.get("enable_order_book") is True]
    executable = [item for item in records if item.get("has_executable_book")]
    short_crypto = [item for item in records if item.get("is_short_crypto_candidate")]
    short_executable = [item for item in short_crypto if item.get("has_executable_book")]
    tradable = [item for item in records if item.get("tradable_clob")]

    def sort_key(item: dict[str, Any]) -> tuple[float, float]:
        try:
            liquidity = float(item.get("liquidity_clob") or 0)
        except Exception:
            liquidity = 0.0
        try:
            volume = float(item.get("volume_24hr_clob") or 0)
        except Exception:
            volume = 0.0
        return (liquidity, volume)

    tradable_sorted = sorted(tradable, key=sort_key, reverse=True)
    short_window = sorted(
        [item for item in records if item.get("window_guess") in {"5m", "10m", "15m", "hour"}],
        key=lambda item: (item.get("seconds_to_close") if item.get("seconds_to_close") is not None else 10**12),
    )
    summary = {
        "total_gamma_candidates": len(records),
        "clob_enabled_candidates": len(clob_enabled),
        "executable_books": len(executable),
        "short_crypto_candidates": len(short_crypto),
        "short_crypto_executable_books": len(short_executable),
        "tradable_clob_markets": len(tradable),
    }
    summary["diagnosis"] = diagnose_tradable_crypto_discovery(summary)
    return {
        "status": "ok",
        "summary": summary,
        "top_tradable_crypto_markets": tradable_sorted[:top_n],
        "all_short_window_candidates": short_window,
        "short_crypto_candidates_detail": short_crypto,
        "notes": {
            "discovery_strategy": "gamma_active_unexpired_crypto_keywords_then_clob_probe",
            "slug_pattern_assumption": False,
            "executable_book_requires_asks": True,
        },
    }


def fetch_gamma_series_by_slug(series_slug: str) -> dict[str, Any] | None:
    if not series_slug:
        return None
    rows = _get_json(f"{DEFAULT_GAMMA_HOST}/series?{urlencode({'slug': series_slug})}")
    if isinstance(rows, list) and rows:
        return rows[0]
    return None


def series_live_event_markets(series_slug: str, *, now: datetime | None = None) -> list[dict[str, Any]]:
    series = fetch_gamma_series_by_slug(series_slug)
    if not series:
        return []
    now = now or datetime.now(timezone.utc)
    markets: list[dict[str, Any]] = []
    for event in series.get("events") or []:
        slug = str(event.get("slug") or "")
        if not slug:
            continue
        market = gamma_market_by_slug(slug)
        if not market:
            continue
        enriched = dict(market)
        enriched["event_id"] = event.get("id")
        enriched["series_slug"] = series_slug
        enriched["series_id"] = series.get("id")
        if enriched.get("enableOrderBook") is None:
            enriched["enableOrderBook"] = event.get("enableOrderBook")
        if enriched.get("acceptingOrders") is None:
            enriched["acceptingOrders"] = event.get("acceptingOrders")
        if enriched.get("endDate") is None:
            enriched["endDate"] = event.get("endDate")
        markets.append(enriched)

    def _sort_key(item: dict[str, Any]) -> tuple[int, int]:
        seconds = _market_seconds_to_close(item, now=now)
        expired = 1 if seconds is not None and seconds <= 0 else 0
        return (expired, seconds if seconds is not None else 10**18)

    markets.sort(key=_sort_key)
    return markets


def _gamma_endpoint_probe(endpoint: str, url: str) -> dict[str, Any]:
    try:
        payload = _get_json(url)
        found = bool(payload)
        if isinstance(payload, list):
            found = len(payload) > 0
        elif isinstance(payload, dict):
            found = bool(payload.get("events") or payload.get("markets") or payload.get("id"))
        return {"endpoint": endpoint, "status": 200, "found": found, "error": None, "payload_type": type(payload).__name__}
    except HTTPError as exc:
        return {"endpoint": endpoint, "status": exc.code, "found": False, "error": str(exc), "payload_type": None}
    except Exception as exc:
        return {"endpoint": endpoint, "status": None, "found": False, "error": str(exc), "payload_type": None}


def _extract_ids_from_gamma_payload(payload: Any, target_slug: str) -> dict[str, Any]:
    ids: dict[str, Any] = {
        "event_id": None,
        "market_id": None,
        "condition_id": None,
        "clobTokenIds": None,
        "series_slug": None,
        "series_id": None,
    }
    candidates: list[dict[str, Any]] = []
    if isinstance(payload, list):
        candidates.extend(item for item in payload if isinstance(item, dict))
    elif isinstance(payload, dict):
        if payload.get("slug") == target_slug or payload.get("markets") or payload.get("events"):
            if payload.get("slug") == target_slug:
                candidates.append(payload)
            for key in ("markets", "events"):
                for item in payload.get(key) or []:
                    if isinstance(item, dict):
                        candidates.append(item)
        else:
            candidates.append(payload)
    for item in candidates:
        slug = str(item.get("slug") or "")
        if slug and slug != target_slug and "markets" not in item and "events" not in item:
            continue
        if slug == target_slug or not ids.get("market_id"):
            ids["event_id"] = ids["event_id"] or item.get("id")
            ids["market_id"] = item.get("id") if item.get("clobTokenIds") or item.get("conditionId") else ids["market_id"]
            ids["condition_id"] = ids["condition_id"] or item.get("conditionId") or item.get("condition_id")
            ids["clobTokenIds"] = ids["clobTokenIds"] or item.get("clobTokenIds") or item.get("clob_token_ids")
            ids["series_slug"] = ids["series_slug"] or item.get("seriesSlug") or item.get("series_slug")
            ids["series_id"] = ids["series_id"] or item.get("seriesId") or item.get("series_id")
        for sub in item.get("markets") or []:
            if str(sub.get("slug") or "") == target_slug:
                ids["market_id"] = sub.get("id")
                ids["condition_id"] = sub.get("conditionId") or sub.get("condition_id")
                ids["clobTokenIds"] = sub.get("clobTokenIds") or sub.get("clob_token_ids")
    return ids


def resolve_gamma_event_slug(slug: str) -> dict[str, Any]:
    slug = str(slug).strip()
    endpoint_results: list[dict[str, Any]] = []
    resolved_market: dict[str, Any] | None = None
    resolved_event: dict[str, Any] | None = None
    series_info: dict[str, Any] | None = None

    probes = [
        ("gamma_markets_slug", f"{DEFAULT_GAMMA_HOST}/markets?{urlencode({'slug': slug})}"),
        ("gamma_events_slug", f"{DEFAULT_GAMMA_HOST}/events?{urlencode({'slug': slug})}"),
        ("gamma_public_search", f"{DEFAULT_GAMMA_HOST}/public-search?{urlencode({'q': slug})}"),
        ("gamma_markets_slug_contains", f"{DEFAULT_GAMMA_HOST}/markets?{urlencode({'slug_contains': slug.split('-')[0]})}"),
    ]
    for name, url in probes:
        result = _gamma_endpoint_probe(name, url)
        endpoint_results.append(result)
        if not result.get("found"):
            continue
        try:
            payload = _get_json(url)
        except Exception:
            continue
        if name == "gamma_markets_slug" and isinstance(payload, list) and payload:
            resolved_market = payload[0]
        if name == "gamma_events_slug" and isinstance(payload, list) and payload:
            resolved_event = payload[0]
            for sub in resolved_event.get("markets") or []:
                if str(sub.get("slug") or "") == slug:
                    resolved_market = sub
                    break

    if resolved_event and resolved_event.get("seriesSlug"):
        series = fetch_gamma_series_by_slug(str(resolved_event.get("seriesSlug")))
        if series:
            series_info = {"series_slug": series.get("slug"), "series_id": series.get("id"), "embedded_events": len(series.get("events") or [])}
            endpoint_results.append(
                {
                    "endpoint": "gamma_series_by_slug",
                    "status": 200,
                    "found": True,
                    "error": None,
                    "payload_type": "series",
                    "series_slug": series.get("slug"),
                }
            )

    if resolved_market is None:
        market = gamma_market_by_slug(slug)
        if market:
            resolved_market = market
            endpoint_results.append(
                {"endpoint": "gamma_market_by_slug_helper", "status": 200, "found": True, "error": None, "payload_type": "market"}
            )

    token_ids = discover_candidate_token_ids(resolved_market or {})
    token_probes = [probe_clob_token_tradable(token_id) for token_id in token_ids] if resolved_market else []
    return {
        "slug": slug,
        "endpoint_results": endpoint_results,
        "resolved_event": resolved_event,
        "resolved_market": resolved_market,
        "series_info": series_info,
        "ids": {
            "event_id": (resolved_event or {}).get("id"),
            "market_id": (resolved_market or {}).get("id"),
            "condition_id": _market_condition_id(resolved_market or {}),
            "clobTokenIds": (resolved_market or {}).get("clobTokenIds") or (resolved_market or {}).get("clob_token_ids"),
            "series_slug": (resolved_event or {}).get("seriesSlug") or (series_info or {}).get("series_slug"),
            "series_id": (series_info or {}).get("series_id"),
        },
        "market_flags": {
            "active": (resolved_market or {}).get("active"),
            "closed": (resolved_market or {}).get("closed"),
            "archived": (resolved_market or {}).get("archived"),
            "enable_order_book": _market_enable_order_book(resolved_market or {}),
            "accepting_orders": _market_accepting_orders(resolved_market or {}),
            "endDate": (resolved_market or {}).get("endDate") or (resolved_market or {}).get("end_date"),
            "seconds_to_close": _market_seconds_to_close(resolved_market or {}),
            "outcomes": (resolved_market or {}).get("outcomes"),
        },
        "token_ids": token_ids,
        "token_probes": token_probes,
        "has_executable_book": any(item.get("book", {}).get("executable_book") for item in token_probes),
    }


def explain_broad_scanner_miss(slug: str, resolved_market: dict[str, Any] | None) -> dict[str, Any]:
    market = resolved_market or gamma_market_by_slug(slug) or {}
    reasons: list[str] = []
    if not market:
        reasons.append("slug_not_in_broad_scanner_feed")
        return {"reasons": reasons, "market_found": False}
    if not market_looks_crypto_related(market):
        reasons.append("crypto_keyword_filter")
    if not gamma_market_is_active_unexpired(market):
        reasons.append("expired_or_inactive_filter")
    if _market_enable_order_book(market) is False:
        reasons.append("enable_order_book_false")
    in_tradable = any(str(item.get("slug") or "") == slug for item in collect_gamma_tradable_crypto_candidates(max_pages=5))
    if not in_tradable:
        reasons.append("not_in_gamma_pagination_scan")
    in_legacy = any(str(item.get("slug") or "") == slug for asset in SHORT_CRYPTO_ASSETS for item in _gamma_markets(asset=asset, window_minutes=5, limit=500))
    if not in_legacy:
        reasons.append("not_in_crypto_events_tag_feed")
    in_series = any(
        str(item.get("slug") or "") == slug
        for asset in SHORT_CRYPTO_ASSETS
        for window in SHORT_CRYPTO_WINDOWS
        for series_slug in [SHORT_CRYPTO_SERIES_BY_ASSET.get(asset, {}).get(window)]
        if series_slug
        for item in series_live_event_markets(series_slug)
    )
    if not in_series:
        reasons.append("series_endpoint_not_used_by_old_scanner")
    return {
        "reasons": reasons,
        "market_found": True,
        "primary_miss_cause": reasons[0] if reasons else None,
    }


def audit_polymarket_event_slug(slug: str) -> dict[str, Any]:
    resolution = resolve_gamma_event_slug(slug)
    scanner = explain_broad_scanner_miss(slug, resolution.get("resolved_market"))
    winning = [item for item in resolution.get("endpoint_results") or [] if item.get("found")]
    return {
        "status": "ok" if resolution.get("resolved_market") else "not_found",
        "slug": slug,
        "endpoints_tested": resolution.get("endpoint_results"),
        "winning_endpoints": [item.get("endpoint") for item in winning],
        "resolved": resolution,
        "scanner_comparison": scanner,
        "diagnosis": (
            "resolved_with_executable_book"
            if resolution.get("has_executable_book")
            else "resolved_but_no_executable_book"
            if resolution.get("resolved_market")
            else "slug_not_found"
        ),
    }

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
