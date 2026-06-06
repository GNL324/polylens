from __future__ import annotations

import os
import time
import uuid
from dataclasses import replace
from typing import Any

from src.adapters.kalshi import KalshiAuthenticatedClient
from src.risk import RiskEngine, RiskRequest
from src.risk.models import RiskConfig as PersistentRiskConfig, RiskDecisionType


def live_smoke_test_gates(*, ticker: str | None, side: str | None, price: float | None, count: int | None, max_notional: float = 1.0) -> tuple[bool, str, dict[str, Any]]:
    if os.environ.get("LIVE_TRADING", "false").lower() != "true":
        return False, "LIVE_TRADING=true is required", {}
    if os.environ.get("DRY_RUN", "true").lower() != "false":
        return False, "DRY_RUN=false is required", {}
    if os.environ.get("KALSHI_LIVE_SMOKE_TEST", "false").lower() != "true":
        return False, "KALSHI_LIVE_SMOKE_TEST=true is required", {}
    try:
        signal = _normalize_smoke_signal(ticker=ticker, side=side, price=price, count=count)
    except ValueError as exc:
        return False, str(exc), {}
    notional = signal["price"] * signal["count"]
    if notional > max_notional:
        return False, "notional exceeds max-notional", {"notional": round(notional, 4), "max_notional": max_notional}
    return True, "accepted", {**signal, "notional": round(notional, 4), "max_notional": max_notional}


def run_kalshi_live_smoke_test(
    *,
    ticker: str,
    side: str,
    price: float,
    count: int,
    max_notional: float = 1.0,
    client: KalshiAuthenticatedClient | None = None,
    risk_engine: RiskEngine | None = None,
) -> dict[str, Any]:
    risk_engine = risk_engine or RiskEngine()
    request_id = f"smoke-{int(time.time())}-{uuid.uuid4().hex[:8]}"
    raw_metadata = {"source": "kalshi_live_smoke_test", "side": side, "price": price, "count": count, "max_notional": max_notional}
    risk_engine.record_event(
        "smoke_test_requested",
        venue="kalshi",
        market=ticker or "unknown",
        opportunity_id=request_id,
        decision=RiskDecisionType.APPROVE,
        reason="smoke test requested",
        dry_run=os.environ.get("DRY_RUN", "true").lower() != "false",
        live_trading=os.environ.get("LIVE_TRADING", "false").lower() == "true",
        metadata=raw_metadata,
    )
    accepted, reason, signal = live_smoke_test_gates(ticker=ticker, side=side, price=price, count=count, max_notional=max_notional)
    if not accepted:
        risk_engine.record_event(
            "smoke_test_blocked",
            venue="kalshi",
            market=ticker or "unknown",
            opportunity_id=request_id,
            decision=RiskDecisionType.REJECT,
            reason=reason,
            proposed_stake=float((signal or {}).get("notional") or 0.0),
            dry_run=os.environ.get("DRY_RUN", "true").lower() != "false",
            live_trading=os.environ.get("LIVE_TRADING", "false").lower() == "true",
            severity="warning",
            metadata={**raw_metadata, "risk": signal},
        )
        return {"accepted": False, "mode": "blocked", "reason": reason, **({"risk": signal} if signal else {})}
    base_risk_config = risk_engine.config
    smoke_risk_config = replace(
        base_risk_config,
        max_position_size=max_notional,
        min_opportunity_score=0.0,
        min_expected_edge=0.0,
        duplicate_opportunity_cooldown_seconds=0,
        dry_run=True,
        live_trading=False,
    )
    risk_engine.config = smoke_risk_config
    risk_decision = risk_engine.evaluate(
        RiskRequest(
            venue="kalshi",
            market=signal["ticker"],
            opportunity_id=request_id,
            proposed_stake=signal["notional"],
            score=1.0,
            edge=0.0,
            dry_run=True,
            live_trading=False,
            metadata={**raw_metadata, "event_type": "smoke_test_risk_check", "actual_dry_run": False, "actual_live_trading": True},
        )
    )
    if risk_decision.decision != RiskDecisionType.APPROVE:
        risk_engine.record_event(
            "smoke_test_blocked",
            venue="kalshi",
            market=signal["ticker"],
            opportunity_id=request_id,
            decision=risk_decision.decision,
            reason=risk_decision.reason,
            proposed_stake=signal["notional"],
            current_exposure=risk_decision.current_exposure,
            daily_pnl=risk_decision.daily_pnl,
            monthly_pnl=risk_decision.monthly_pnl,
            dry_run=False,
            live_trading=True,
            severity="warning",
            metadata={**raw_metadata, "risk_decision": risk_decision.to_dict()},
        )
        return {"accepted": False, "mode": "blocked", "reason": risk_decision.reason, "risk_decision": risk_decision.to_dict()}
    client = client or KalshiAuthenticatedClient(raw_dir="data/raw")
    client_order_id = f"polylens-smoke-{int(time.time())}-{uuid.uuid4().hex[:12]}"
    created = client.create_order_v2_smoke_test_only(signal["ticker"], signal["side"], signal["price"], signal["count"], client_order_id=client_order_id)
    order = _extract_order(created)
    order_id = _order_id(order) or _order_id(created)
    if not order_id:
        risk_engine.record_event(
            "smoke_test_failed",
            venue="kalshi",
            market=signal["ticker"],
            opportunity_id=request_id,
            decision=RiskDecisionType.REJECT,
            reason="created order response did not include order id",
            proposed_stake=signal["notional"],
            dry_run=False,
            live_trading=True,
            severity="high",
            metadata={**raw_metadata, "client_order_id": client_order_id, "create_response": _safe_response(created)},
        )
        return {"accepted": False, "mode": "create_failed", "reason": "created order response did not include order id", "create_response": _safe_response(created), "client_order_id": client_order_id}
    risk_engine.record_event(
        "smoke_test_order_created",
        venue="kalshi",
        market=signal["ticker"],
        opportunity_id=request_id,
        decision=RiskDecisionType.APPROVE,
        reason="smoke test order created",
        proposed_stake=signal["notional"],
        dry_run=False,
        live_trading=True,
        metadata={**raw_metadata, "client_order_id": client_order_id, "order_id": order_id},
    )
    verified_open = client.get_order_v2_smoke_test_only(order_id)
    open_order = _extract_order(verified_open)
    partial = _partial_fill_detected(open_order) or _partial_fill_detected(order)
    if partial:
        risk_engine.record_event(
            "smoke_test_partial_fill_detected",
            venue="kalshi",
            market=signal["ticker"],
            opportunity_id=request_id,
            decision=RiskDecisionType.HALT,
            reason="order was partially filled during smoke test; review account immediately",
            proposed_stake=signal["notional"],
            dry_run=False,
            live_trading=True,
            severity="high",
            metadata={**raw_metadata, "client_order_id": client_order_id, "order_id": order_id, "open_order": _safe_response(open_order)},
        )
    cancel_response = client.cancel_order_v2_smoke_test_only(order_id)
    risk_engine.record_event(
        "smoke_test_order_cancelled",
        venue="kalshi",
        market=signal["ticker"],
        opportunity_id=request_id,
        decision=RiskDecisionType.APPROVE,
        reason="smoke test cancel requested",
        proposed_stake=signal["notional"],
        dry_run=False,
        live_trading=True,
        metadata={**raw_metadata, "client_order_id": client_order_id, "order_id": order_id, "cancel_response": _safe_response(cancel_response)},
    )
    verified_cancel = client.get_order_v2_smoke_test_only(order_id)
    canceled_order = _extract_order(verified_cancel)
    canceled = _is_canceled(canceled_order) or _is_canceled(cancel_response)
    result = {
        "accepted": not partial and canceled,
        "mode": "live_smoke_test",
        "client_order_id": client_order_id,
        "order_id": order_id,
        "ticker": signal["ticker"],
        "side": signal["side"],
        "price": signal["price"],
        "count": signal["count"],
        "notional": signal["notional"],
        "post_only": True,
        "created": True,
        "verified_open": bool(open_order or verified_open),
        "cancel_requested": True,
        "verified_canceled": canceled,
        "partial_fill_detected": partial,
    }
    if partial:
        result["accepted"] = False
        result["mode"] = "partial_fill_detected"
        result["reason"] = "order was partially filled during smoke test; review account immediately"
    elif not canceled:
        result["accepted"] = False
        result["mode"] = "cancel_verification_failed"
        result["reason"] = "cancel was requested but canceled status was not verified"
        risk_engine.record_event(
            "smoke_test_failed",
            venue="kalshi",
            market=signal["ticker"],
            opportunity_id=request_id,
            decision=RiskDecisionType.REJECT,
            reason=result["reason"],
            proposed_stake=signal["notional"],
            dry_run=False,
            live_trading=True,
            severity="high",
            metadata={**raw_metadata, "client_order_id": client_order_id, "order_id": order_id, "verified_cancel": _safe_response(canceled_order)},
        )
    return result


def _extract_order(payload: dict[str, Any]) -> dict[str, Any]:
    for key in ("order", "order_response"):
        value = payload.get(key) if isinstance(payload, dict) else None
        if isinstance(value, dict):
            return value
    return payload if isinstance(payload, dict) else {}


def _order_id(order: dict[str, Any]) -> str | None:
    for key in ("order_id", "id", "orderId"):
        value = order.get(key)
        if value:
            return str(value)
    return None


def _partial_fill_detected(order: dict[str, Any]) -> bool:
    for key in ("filled_count", "fill_count", "filled_quantity", "filled_qty"):
        try:
            if float(order.get(key) or 0) > 0:
                return True
        except (TypeError, ValueError):
            continue
    status = str(order.get("status") or "").lower()
    return status in {"partially_filled", "filled", "executed"}


def _is_canceled(order: dict[str, Any]) -> bool:
    status = str(order.get("status") or order.get("state") or "").lower()
    return status in {"canceled", "cancelled"}


def _safe_response(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    allowed = {"order", "order_id", "id", "status", "client_order_id", "error", "message"}
    return {key: value for key, value in payload.items() if key in allowed}



def _normalize_smoke_signal(*, ticker: str | None, side: str | None, price: float | None, count: int | None) -> dict[str, Any]:
    ticker_value = str(ticker or "").strip()
    if not ticker_value:
        raise ValueError("ticker is required")
    side_value = str(side or "").lower().strip()
    if side_value not in {"yes", "no"}:
        raise ValueError("side must be yes or no")
    try:
        count_value = int(count)
    except (TypeError, ValueError):
        raise ValueError("count must be a positive integer") from None
    if count_value <= 0:
        raise ValueError("count must be a positive integer")
    price_value = _smoke_price_to_dollars(price)
    return {"ticker": ticker_value, "side": side_value, "price": price_value, "count": count_value}


def _smoke_price_to_dollars(price: float | None) -> float:
    try:
        numeric = float(price)
    except (TypeError, ValueError):
        raise ValueError("price must be numeric") from None
    if numeric <= 0:
        raise ValueError("price must be positive")
    if numeric < 1:
        dollars = numeric
    else:
        dollars = numeric / 100.0
    if dollars <= 0 or dollars >= 1:
        raise ValueError("price must be between 1 and 99 cents")
    return round(dollars, 4)
