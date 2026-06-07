from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

from src.adapters.kalshi import KalshiAuthConfig, KalshiAuthConfigError
from src.analysis.short_crypto_executor import (
    ShortCryptoExecutor,
    ShortCryptoRiskConfig,
    build_kalshi_order_payload,
    first_live_test_dedupe_context,
    first_live_test_run_id,
    live_trade_dedupe_key,
    select_executable_yes_ask,
)
from src.analysis.short_crypto_executor import ShortCryptoSignalEngine
from src.analysis.short_crypto_markets import normalize_short_crypto_markets
from src.adapters.kalshi import KalshiClient
from src.cli import _coinbase_rest_spot_map, _load_kalshi_short_crypto_markets


def main() -> None:
    logging.disable(logging.CRITICAL)
    os.environ.setdefault("POLYLENS_FIRST_LIVE_TEST", "true")
    payload = build_audit()
    print(json.dumps(payload, indent=2, sort_keys=True, default=str))


def build_audit() -> dict[str, Any]:
    audit = _audit_btc_selection()
    rejected_candidates = audit["rejected_candidates"]
    if audit["signal"] is None:
        return {
            "status": "not_ready",
            "reason": "no_executable_kalshi_candidate_found",
            "sent": False,
            "candidates_checked": audit["candidates_checked"],
            "rejected_candidates": rejected_candidates,
            "selected_candidate": None,
        }

    signal = audit["signal"]
    market = signal.meta.get("market") if isinstance(signal.meta, dict) else None
    selected = _selected_yes_ask(market)
    executor = ShortCryptoExecutor(config=ShortCryptoRiskConfig.from_env())
    first_live_test = _env_true("POLYLENS_FIRST_LIVE_TEST")
    stake = min(executor._sized_stake(signal), 1.0) if first_live_test else executor._sized_stake(signal)
    equivalent_yes_price_cents = int(selected["price_cents"])
    equivalent_yes_price = equivalent_yes_price_cents / 100.0
    count = 1 if first_live_test else max(1, int(stake / equivalent_yes_price))
    count = min(count, int(selected["count"]))
    order_intent = executor._order_intent(signal, stake=stake, count=count, mode="live")
    order_intent["price"] = equivalent_yes_price
    order_intent["selected_liquidity_source"] = selected.get("derived_from")
    if selected.get("derived_from") == "no_bid":
        order_intent["action"] = "sell"
        order_intent["side"] = "no"
        order_intent.pop("yes_price_cents", None)
        order_intent["no_price_cents"] = int(selected["no_bid_cents"])
    else:
        order_intent["action"] = "buy"
        order_intent["side"] = "yes"
        order_intent["yes_price_cents"] = equivalent_yes_price_cents
    order_intent["kalshi_payload"] = build_kalshi_order_payload(order_intent)
    order_payload = order_intent["kalshi_payload"]
    max_loops = 1
    trade_key = live_trade_dedupe_key(signal, mode="live", count=count, max_loops=max_loops)
    dedupe_context = first_live_test_dedupe_context(mode="live", count=count, max_loops=max_loops)
    duplicate_exists = executor._dedupe.exists(trade_key)

    signing = _signing_config_status()
    payload_validation = _validate_payload(
        order_payload,
        first_live_test=first_live_test,
        stake=stake,
        price=equivalent_yes_price,
        selected=selected,
    )
    gate_reason = executor._validate(signal, stake, trade_key, mode="live", require_env=True, count=count, max_loops=max_loops)
    live_gates = {"ok": gate_reason is None, "reason": gate_reason}
    economic_exposure = {
        "type": "equivalent_yes_buy",
        "yes_price_cents": equivalent_yes_price_cents,
        "yes_price": equivalent_yes_price,
        "count": count,
        "max_exposure_cents": count * equivalent_yes_price_cents,
        "max_exposure_dollars": round((count * equivalent_yes_price_cents) / 100.0, 4),
    }

    return {
        "status": "ready" if signing["ok"] and payload_validation["ok"] and live_gates["ok"] else "not_ready",
        "sent": False,
        "order_endpoint_called": False,
        "dedupe_key": trade_key,
        "first_live_test_run_id": first_live_test_run_id(),
        "duplicate_status": {
            "duplicate": duplicate_exists,
            "first_live_test_run_id_required": bool(dedupe_context["enabled"]),
            "run_id_included_in_dedupe_key": bool(dedupe_context["run_id"]),
            "reason": "duplicate_trade_key" if duplicate_exists else None,
        },
        "candidates_checked": audit["candidates_checked"],
        "rejected_candidates": rejected_candidates,
        "selected_candidate": {
            "ticker": signal.ticker,
            "reason": "selected",
            "selected_liquidity_source": "no_bid_ladder" if selected.get("derived_from") == "no_bid" else selected.get("derived_from"),
            "selected_ask_price": equivalent_yes_price,
            "selected_ask_count": selected["count"],
            "selected_ask_raw": selected["raw"],
            "selected_no_bid_cents": selected.get("no_bid_cents"),
        },
        "ticker": signal.ticker,
        "title": _market_field(market, "title"),
        "strike": getattr(market, "strike_price", None),
        "current_spot": signal.spot_price,
        "yes_bid": getattr(market, "yes_bid", None),
        "yes_ask": getattr(market, "yes_ask", None),
        "selected_liquidity_source": "no_bid_ladder" if selected.get("derived_from") == "no_bid" else selected.get("derived_from"),
        "economic_exposure": economic_exposure,
        "selected_ask_price": equivalent_yes_price,
        "selected_ask_count": selected["count"],
        "selected_ask_raw": selected["raw"],
        "implied_probability": signal.implied_prob,
        "model_probability": signal.model_prob,
        "edge": signal.edge,
        "exact_order_payload": order_payload,
        "validations": {
            "first_live_test_mode": {"ok": first_live_test, "env": "POLYLENS_FIRST_LIVE_TEST", "value": os.environ.get("POLYLENS_FIRST_LIVE_TEST")},
            "signing_configuration_present": signing,
            "payload_valid": payload_validation,
            "would_pass_live_gates": live_gates,
        },
    }


def _signing_config_status() -> dict[str, Any]:
    try:
        KalshiAuthConfig.from_env()
        return {"ok": True, "reason": None, "credentials_detected_not_printed": True}
    except KalshiAuthConfigError as exc:
        return {
            "ok": False,
            "reason": str(exc),
            "credentials_detected_not_printed": bool(os.environ.get("KALSHI_API_KEY_ID") or os.environ.get("KALSHI_PRIVATE_KEY_PATH")),
        }


def _validate_payload(payload: dict[str, Any], *, first_live_test: bool, stake: float, price: float, selected: dict[str, Any]) -> dict[str, Any]:
    required = {"ticker", "side", "action", "type", "count", "time_in_force", "client_order_id"}
    missing = sorted(required - set(payload))
    price_keys = [key for key in ("yes_price", "no_price") if key in payload]
    problems = []
    if missing:
        problems.append(f"missing_required_fields:{','.join(missing)}")
    if len(price_keys) != 1:
        problems.append("exactly_one_yes_price_or_no_price_required")
    if payload.get("side") not in {"yes", "no"}:
        problems.append("side_must_be_yes_or_no")
    if payload.get("action") not in {"buy", "sell"}:
        problems.append("action_must_be_buy_or_sell")
    if int(payload.get("count") or 0) <= 0:
        problems.append("count_must_be_positive")
    count = int(payload.get("count") or 0)
    buy_max_cost = int(payload.get("buy_max_cost") or 0)
    action = payload.get("action")
    side = payload.get("side")
    if action == "buy":
        if buy_max_cost <= 0:
            problems.append("buy_max_cost_must_be_positive_for_buy")
    if action == "sell":
        if "buy_max_cost" in payload:
            problems.append("sell_payload_must_not_include_buy_max_cost")
        if side == "no" and "no_price" not in payload:
            problems.append("sell_no_into_no_bid_must_include_no_price")
    if first_live_test and count > 1:
        problems.append("first_live_test_reject_count_gt_1")
    if action == "buy" and buy_max_cost > 100:
        problems.append("buy_max_cost_gt_100")
    for key in price_keys:
        value = int(payload.get(key) or 0)
        if value < 1 or value > 99:
            problems.append(f"{key}_must_be_1_to_99_cents")
        if action == "buy" and buy_max_cost != count * value:
            problems.append("buy_max_cost_must_equal_count_times_price")
        if action == "buy" and value == 1 and (count != 1 or buy_max_cost != 1):
            problems.append("one_cent_first_live_order_must_be_one_contract_one_cent")
    if selected.get("derived_from") == "no_bid":
        expected_no_price = int(selected.get("no_bid_cents") or 0)
        if action != "sell" or side != "no":
            problems.append("no_bid_ladder_liquidity_must_use_sell_no")
        if int(payload.get("no_price") or 0) != expected_no_price:
            problems.append("sell_no_price_must_equal_best_no_bid")
    dollars_probability_count = max(1, int(stake / max(price, 0.01)))
    if first_live_test and dollars_probability_count > 1 and count == dollars_probability_count:
        problems.append("count_appears_derived_from_dollars_divided_by_probability")
    equivalent_exposure_cents = count * int(selected.get("price_cents") or 0)
    if first_live_test and equivalent_exposure_cents > 100:
        problems.append("first_live_test_equivalent_exposure_gt_1_dollar")
    return {
        "ok": not problems,
        "problems": problems,
        "max_equivalent_exposure_dollars": equivalent_exposure_cents / 100.0,
        "first_live_test": first_live_test,
    }


def _env_true(name: str) -> bool:
    value = os.environ.get(name)
    return bool(value and value.lower() in {"1", "true", "yes", "on"})


def _market_field(market: Any, key: str) -> Any:
    raw = getattr(market, "raw", None) or {}
    identity = raw.get("market_identity") or {}
    return raw.get(key) or identity.get(key)


def _selected_yes_ask(market: Any) -> dict[str, Any]:
    raw = getattr(market, "raw", None) or {}
    selected = raw.get("selected_yes_ask")
    if isinstance(selected, dict):
        return selected
    return select_executable_yes_ask(raw.get("orderbook") or {})


def _audit_btc_selection() -> dict[str, Any]:
    client = KalshiClient(raw_dir="data/raw")
    markets = normalize_short_crypto_markets(_load_kalshi_short_crypto_markets(["BTC"]), [])
    spot_map = _coinbase_rest_spot_map(["BTC"])
    rejected = []
    candidates_checked = 0
    first_live_test = _env_true("POLYLENS_FIRST_LIVE_TEST")
    for market in markets:
        candidates_checked += 1
        if _close_cutoff_breached(market):
            rejected.append(_reject(market, "market_close_cutoff_breached"))
            continue
        try:
            book = client.get_orderbook(market.ticker)
        except Exception as exc:
            rejected.append(_reject(market, "orderbook_fetch_failed", {"detail": str(exc)}))
            continue
        selected = select_executable_yes_ask(book)
        if not selected.get("ok"):
            rejected.append(_reject(market, "no_executable_resting_yes_ask"))
            continue
        if selected["count"] < 1:
            rejected.append(_reject(market, "selected_ask_count_lt_1", {"selected": selected}))
            continue
        if selected["price_cents"] < 1 or selected["price_cents"] > 99:
            rejected.append(_reject(market, "selected_yes_price_out_of_range", {"selected": selected}))
            continue
        count = 1 if first_live_test else min(1, int(selected["count"]))
        buy_max_cost = count * int(selected["price_cents"])
        if first_live_test and buy_max_cost > 100:
            rejected.append(_reject(market, "first_live_test_buy_max_cost_gt_100", {"selected": selected, "buy_max_cost": buy_max_cost}))
            continue
        if not _spread_liquidity_valid(market, selected):
            rejected.append(_reject(market, "spread_or_liquidity_invalid", {"selected": selected}))
            continue
        enriched = type(market)(
            asset=market.asset,
            venue=market.venue,
            ticker=market.ticker,
            start_ts=market.start_ts,
            end_ts=market.end_ts,
            direction=market.direction,
            yes_bid=market.yes_bid,
            yes_ask=selected["price_cents"] / 100.0,
            no_bid=market.no_bid,
            no_ask=market.no_ask,
            liquidity=market.liquidity if market.liquidity and market.liquidity > 0 else 1.0,
            window_minutes=market.window_minutes,
            strike_price=market.strike_price,
            reference_price=market.reference_price,
            timestamp=time.time(),
            raw={**(market.raw or {}), "orderbook": book, "selected_yes_ask": selected},
        )
        if enriched.asset not in spot_map and enriched.strike_price:
            spot_map[enriched.asset] = float(enriched.strike_price)
        signals = ShortCryptoSignalEngine(assets=["BTC"], windows=[5], min_edge=0.0).generate_signals([enriched], spot_map)
        if signals:
            return {"signal": signals[0], "candidates_checked": candidates_checked, "rejected_candidates": rejected}
        rejected.append(_reject(market, "signal_generation_failed", {"selected": selected}))
    return {"signal": None, "candidates_checked": candidates_checked, "rejected_candidates": rejected}


def _reject(market: Any, reason: str, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    row = {"ticker": getattr(market, "ticker", None), "reason": reason}
    if extra:
        row.update(extra)
    return row


def _close_cutoff_breached(market: Any) -> bool:
    end_ts = getattr(market, "end_ts", None)
    if end_ts is None:
        return False
    return (float(end_ts) - time.time()) <= ShortCryptoRiskConfig.from_env().close_cutoff_seconds


def _spread_liquidity_valid(market: Any, selected: dict[str, Any]) -> bool:
    if selected.get("count", 0) < 1:
        return False
    yes_bid = getattr(market, "yes_bid", None)
    yes_ask = selected.get("price_cents", 0) / 100.0
    if yes_bid is not None and yes_bid > yes_ask:
        return False
    return True


if __name__ == "__main__":
    main()
