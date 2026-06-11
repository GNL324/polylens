from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import time
from pathlib import Path
from typing import Any

from src.sqlite_utils import closing_connection

from src.adapters.polymarket_live import (
    BUY,
    PolymarketLiveAdapter,
    best_ask,
    discover_clob_connectivity_market,
    discover_short_crypto_market,
    first_live_duplicate_key,
    validate_first_live_send_gates,
)
from src.analysis.short_crypto_executor import first_live_test_run_id

AUDIT_MODES = ("short_crypto", "clob_connectivity")


def main() -> None:
    logging.disable(logging.CRITICAL)
    os.environ.setdefault("POLYLENS_POLYMARKET_FIRST_LIVE_TEST", "true")
    os.environ.setdefault("POLYLENS_FIRST_LIVE_TEST", "true")
    parser = argparse.ArgumentParser(description="Polymarket live order audit")
    parser.add_argument("--mode", choices=AUDIT_MODES, default="short_crypto")
    args = parser.parse_args()
    print(json.dumps(build_audit(mode=args.mode), indent=2, sort_keys=True, default=str))


def build_audit(db_path: str = "data/polylens.db", mode: str = "short_crypto") -> dict[str, Any]:
    if mode not in AUDIT_MODES:
        raise ValueError(f"unsupported audit mode: {mode}")

    adapter = PolymarketLiveAdapter()
    creds = adapter.credentials_present()
    discovery = (
        discover_short_crypto_market()
        if mode == "short_crypto"
        else discover_clob_connectivity_market()
    )
    failed_gates: list[str] = []
    if not discovery.get("ok"):
        reason = discovery.get("reason", "market_discovery_failed")
        failed_gates.append(reason)
        return _base_not_ready(adapter, creds, failed_gates, discovery, mode=mode)

    market = discovery["market"]
    outcome = discovery["outcome"]
    book = discovery["orderbook"]
    ask = discovery.get("best_ask") or best_ask(book)
    if not ask:
        failed_gates.append("no_executable_ask")
        return _base_not_ready(adapter, creds, failed_gates, discovery, mode=mode)

    first_live = _polymarket_first_live_test_enabled()
    not_short_crypto = bool(discovery.get("not_short_crypto"))
    market_slug = str(market.get("slug") or market.get("marketSlug") or market.get("id") or "")
    condition_id = str(market.get("conditionId") or market.get("condition_id") or book.get("market") or "")
    token_id = outcome["token_id"]
    price = float(ask["price"])
    ask_size = float(ask["size"])
    count = 1 if first_live else min(int(ask_size), 1) or 1
    intended_size = 1.0 if first_live else min(ask_size, 1.0)
    max_exposure = round(price * intended_size, 4)
    order_args = adapter.order_args(token_id=token_id, price=price, size=intended_size, side=BUY)
    run_id = first_live_test_run_id() if first_live else os.environ.get("POLYLENS_FIRST_LIVE_TEST_RUN_ID")
    dedupe_key = first_live_duplicate_key(market_slug=market_slug, token_id=token_id, run_id=run_id or "")
    duplicate = _duplicate_exists(db_path, dedupe_key)
    balance = adapter.get_balance_allowance() if creds["ok"] else {"ok": False, "status": "not_ready", "reason": "credentials_missing"}
    signed = adapter.dry_run_order(
        token_id=token_id,
        price=price,
        size=intended_size,
        side=BUY,
        tick_size=str(book.get("tick_size") or market.get("minimum_tick_size") or "0.01"),
        neg_risk=bool(book.get("neg_risk") or market.get("negRisk") or market.get("neg_risk") or False),
    )
    signing = _signing_config_status(creds, signed)
    payload_validation = _validate_order_payload(
        order_args,
        first_live_test=first_live,
        price=price,
        size=intended_size,
        max_exposure=max_exposure,
        ask_size=ask_size,
    )
    gate_context = {
        "run_id": run_id,
        "max_exposure": max_exposure,
        "orders_count": count,
        "duplicate": duplicate,
        "fresh_orderbook": _book_fresh(book),
        "executable_ask": ask_size >= intended_size and 0 < price < 1,
        "balance_allowance_ok": bool(balance.get("ok")),
        "not_short_crypto": not_short_crypto,
    }
    live_gates = validate_first_live_send_gates(gate_context)
    failed_gates.extend(live_gates["failed_gates"])
    if not signing["ok"]:
        _add_gate(failed_gates, signing["reason"] or "polymarket_live_signing_not_ready", False)
    if not payload_validation["ok"]:
        for problem in payload_validation["problems"]:
            _add_gate(failed_gates, problem, False)

    live_send_allowed = not not_short_crypto and live_gates["ok"] and signing["ok"] and payload_validation["ok"]
    selected = _selected_candidate(
        market_slug=market_slug,
        condition_id=condition_id,
        token_id=token_id,
        outcome_name=outcome["name"],
        price=price,
        ask_size=ask_size,
        ask=ask,
        discovery=discovery,
    )
    economic_exposure = _build_economic_exposure(price=price, size=intended_size, count=count, max_exposure=max_exposure)

    return {
        "status": "ready" if live_send_allowed and not failed_gates else "not_ready",
        "mode": mode,
        "reason": None if live_send_allowed and not failed_gates else (failed_gates[0] if failed_gates else "not_ready"),
        "failed_gates": failed_gates,
        "sent": False,
        "order_endpoint_called": False,
        "post_order_called": bool(getattr(adapter, "_post_order_called", False)),
        "dedupe_key": dedupe_key,
        "first_live_test_run_id": run_id,
        "duplicate_status": {
            "duplicate": duplicate,
            "first_live_test_run_id_required": first_live,
            "run_id_included_in_dedupe_key": bool(run_id),
            "reason": "duplicate_trade_key" if duplicate else None,
        },
        "rejected_candidates": discovery.get("rejected", []),
        "candidates_checked": discovery.get("candidates_checked", []),
        "clob_book_404_token_ids": discovery.get("clob_book_404_token_ids", []),
        "selected_candidate": selected,
        "not_short_crypto": not_short_crypto,
        "purpose": discovery.get("purpose"),
        "live_send_allowed": live_send_allowed,
        "market_slug": market_slug,
        "condition_id": condition_id,
        "token_id": token_id,
        "outcome": outcome["name"],
        "best_bid": _best_bid(book),
        "best_ask": price,
        "ask_size": ask_size,
        "selected_ask_price": price,
        "selected_ask_size": ask_size,
        "selected_ask_raw": ask.get("raw"),
        "intended_side": BUY,
        "intended_price": price,
        "intended_size": intended_size,
        "count": count,
        "max_exposure": max_exposure,
        "economic_exposure": economic_exposure,
        "credentials_detected_not_printed": creds["credentials_detected_not_printed"],
        "funder_detected_not_printed": creds["funder_detected_not_printed"],
        "signature_type": creds["signature_type"],
        "balance_allowance_status": balance,
        "signed_order_status": {key: val for key, val in signed.items() if key not in {"signed_order"}},
        "exact_order_payload": order_args,
        "exact_order_args": order_args,
        "validations": {
            "polymarket_first_live_test_mode": {
                "ok": first_live,
                "env": "POLYLENS_POLYMARKET_FIRST_LIVE_TEST",
                "value": os.environ.get("POLYLENS_POLYMARKET_FIRST_LIVE_TEST"),
            },
            "signing_configuration_present": signing,
            "payload_valid": payload_validation,
            "would_pass_live_gates": {"ok": live_gates["ok"], "reason": live_gates["failed_gates"][0] if live_gates["failed_gates"] else None, "failed_gates": live_gates["failed_gates"]},
        },
    }


def live_readiness_report() -> dict[str, Any]:
    short_audit = build_audit(mode="short_crypto")
    clob_discovery = discover_clob_connectivity_market()
    clob_audit = build_audit(mode="clob_connectivity")

    short_book_ok = short_audit.get("status") == "ready"
    clob_connectivity_ok = bool(clob_discovery.get("ok"))

    checks: list[dict[str, Any]] = [
        {
            "name": "polymarket_short_crypto_clob_book",
            "ok": short_book_ok,
            "reason": None if short_book_ok else _short_crypto_block_reason(short_audit),
            "meta": {
                "candidates_checked": short_audit.get("candidates_checked", []),
                "clob_book_404_token_ids": short_audit.get("clob_book_404_token_ids", []),
            },
        },
        {
            "name": "polymarket_clob_connectivity",
            "ok": clob_connectivity_ok,
            "reason": None if clob_connectivity_ok else clob_discovery.get("reason"),
            "meta": {
                "not_short_crypto": clob_discovery.get("not_short_crypto", False),
                "purpose": clob_discovery.get("purpose"),
                "market_slug": _discovery_market_slug(clob_discovery),
            },
        },
    ]

    for gate in short_audit.get("failed_gates", []):
        if gate in {"no_short_crypto_clob_book_available", "no_executable_ask", "no_executable_polymarket_crypto_candidate_found"}:
            continue
        checks.append({"name": gate, "ok": False, "reason": gate, "meta": {}})

    return {
        "status": short_audit["status"],
        "live_trading_enabled": _env_true("POLYLENS_POLYMARKET_LIVE_SENDS_ENABLED"),
        "checks": checks,
        "audit": short_audit,
        "clob_connectivity_audit": {
            "discovery_ok": clob_connectivity_ok,
            "audit_status": clob_audit.get("status"),
            "mode": clob_audit.get("mode"),
            "not_short_crypto": clob_discovery.get("not_short_crypto", False),
            "purpose": clob_discovery.get("purpose"),
            "market_slug": _discovery_market_slug(clob_discovery),
            "live_send_allowed": clob_audit.get("live_send_allowed", False),
        },
    }


def _polymarket_first_live_test_enabled() -> bool:
    return _env_true("POLYLENS_POLYMARKET_FIRST_LIVE_TEST")


def _signing_config_status(creds: dict[str, Any], signed: dict[str, Any]) -> dict[str, Any]:
    if not creds.get("ok"):
        return {
            "ok": False,
            "reason": "credentials_missing",
            "credentials_detected_not_printed": creds.get("credentials_detected_not_printed", False),
        }
    if not signed.get("ok"):
        return {
            "ok": False,
            "reason": signed.get("reason", "polymarket_live_signing_not_ready"),
            "credentials_detected_not_printed": creds.get("credentials_detected_not_printed", False),
        }
    return {"ok": True, "reason": None, "credentials_detected_not_printed": creds.get("credentials_detected_not_printed", False)}


def _validate_order_payload(
    payload: dict[str, Any],
    *,
    first_live_test: bool,
    price: float,
    size: float,
    max_exposure: float,
    ask_size: float,
) -> dict[str, Any]:
    required = {"token_id", "price", "size", "side"}
    missing = sorted(required - set(payload))
    problems: list[str] = []
    if missing:
        problems.append(f"missing_required_fields:{','.join(missing)}")
    if payload.get("side") not in {BUY, "SELL"}:
        problems.append("side_must_be_buy_or_sell")
    if float(payload.get("price") or 0) <= 0 or float(payload.get("price") or 0) >= 1:
        problems.append("price_must_be_between_0_and_1")
    if float(payload.get("size") or 0) <= 0:
        problems.append("size_must_be_positive")
    if first_live_test and float(payload.get("size") or 0) != 1.0:
        problems.append("first_live_test_reject_size_not_one")
    if first_live_test and ask_size < 1.0:
        problems.append("selected_ask_size_lt_1")
    if first_live_test and max_exposure > 1.0:
        problems.append("first_live_test_max_exposure_gt_1_dollar")
    if first_live_test and round(float(payload.get("price") or 0) * float(payload.get("size") or 0), 4) != max_exposure:
        problems.append("max_exposure_must_equal_price_times_size")
    return {
        "ok": not problems,
        "problems": problems,
        "max_exposure_dollars": max_exposure,
        "first_live_test": first_live_test,
        "count": 1 if first_live_test else size,
    }


def _build_economic_exposure(*, price: float, size: float, count: int, max_exposure: float) -> dict[str, Any]:
    return {
        "type": "clob_buy",
        "price": price,
        "size": size,
        "count": count,
        "max_exposure_dollars": max_exposure,
    }


def _selected_candidate(
    *,
    market_slug: str,
    condition_id: str,
    token_id: str,
    outcome_name: str,
    price: float,
    ask_size: float,
    ask: dict[str, Any],
    discovery: dict[str, Any],
) -> dict[str, Any]:
    candidate_meta = (discovery.get("candidates_checked") or [{}])[0]
    return {
        "market_slug": market_slug,
        "condition_id": condition_id,
        "token_id": token_id,
        "outcome": outcome_name,
        "reason": "selected",
        "selected_ask_price": price,
        "selected_ask_size": ask_size,
        "selected_ask_raw": ask.get("raw"),
        "asset": candidate_meta.get("asset", "BTC"),
        "window_minutes": candidate_meta.get("window_minutes", 5),
    }


def _discovery_market_slug(discovery: dict[str, Any]) -> str | None:
    market = discovery.get("market") or {}
    slug = market.get("slug") or market.get("marketSlug") or market.get("id")
    return str(slug) if slug else None


def _short_crypto_block_reason(audit: dict[str, Any]) -> str | None:
    if audit.get("reason"):
        return str(audit["reason"])
    failed = audit.get("failed_gates") or []
    if "no_short_crypto_clob_book_available" in failed:
        return "no_short_crypto_clob_book_available"
    return failed[0] if failed else "not_ready"


def _base_not_ready(
    adapter: PolymarketLiveAdapter,
    creds: dict[str, Any],
    failed_gates: list[str],
    discovery: dict[str, Any],
    *,
    mode: str,
) -> dict[str, Any]:
    return {
        "status": "not_ready",
        "mode": mode,
        "reason": discovery.get("reason") or (failed_gates[0] if failed_gates else "not_ready"),
        "failed_gates": failed_gates,
        "candidates_checked": discovery.get("candidates_checked", []),
        "clob_book_404_token_ids": discovery.get("clob_book_404_token_ids", []),
        "not_short_crypto": discovery.get("not_short_crypto", False),
        "purpose": discovery.get("purpose"),
        "live_send_allowed": False,
        "selected_candidate": None,
        "credentials_detected_not_printed": creds["credentials_detected_not_printed"],
        "funder_detected_not_printed": creds["funder_detected_not_printed"],
        "signature_type": creds["signature_type"],
        "balance_allowance_status": {"ok": False, "status": "not_ready", "reason": "not_checked"},
        "discovery": discovery,
        "order_endpoint_called": False,
        "post_order_called": bool(getattr(adapter, "_post_order_called", False)),
        "sent": False,
    }


def _duplicate_exists(db_path: str, key: str) -> bool:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    with closing_connection(db_path, row_factory=None) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS polymarket_first_live_keys (dedupe_key TEXT PRIMARY KEY, created_at REAL NOT NULL)")
        row = conn.execute("SELECT 1 FROM polymarket_first_live_keys WHERE dedupe_key=? LIMIT 1", (key,)).fetchone()
    return row is not None


def _add_gate(failed: list[str], reason: str, ok: bool) -> None:
    if not ok and reason not in failed:
        failed.append(reason)


def _book_fresh(book: dict[str, Any]) -> bool:
    try:
        ts = float(book.get("timestamp") or 0)
        if ts > 10_000_000_000:
            ts = ts / 1000.0
        return ts > 0 and (time.time() - ts) <= float(os.environ.get("POLYLENS_SHORT_CRYPTO_BOOK_STALENESS_SECS", "10"))
    except Exception:
        return False


def _best_bid(book: dict[str, Any]) -> float | None:
    bids = []
    for raw in book.get("bids") or []:
        try:
            bids.append(float(raw.get("price") if isinstance(raw, dict) else raw[0]))
        except Exception:
            continue
    return max(bids) if bids else None


def _env_true(name: str) -> bool:
    value = os.environ.get(name)
    return bool(value and value.lower() in {"1", "true", "yes", "on"})


if __name__ == "__main__":
    main()
