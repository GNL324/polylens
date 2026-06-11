from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from src.adapters.polymarket_live import (
    DEFAULT_CLOB_HOST,
    DEFAULT_GAMMA_HOST,
    _get_json,
    _json_list,
    _market_condition_id,
    resolve_gamma_event_slug,
)
from urllib.parse import urlencode


def resolve_polymarket_short_crypto_market(
    slug: str,
    *,
    include_diagnostics: bool = False,
) -> dict[str, Any]:
    diagnostics = _probe_settlement_sources(slug) if include_diagnostics else None
    resolution = _resolve_from_gamma(slug)
    if resolution.get("resolved"):
        payload = {
            "resolved": True,
            "outcome": resolution["outcome"],
            "source": resolution["source"],
            "confidence": resolution["confidence"],
            "settlement_timestamp": resolution.get("settlement_timestamp"),
        }
        if include_diagnostics:
            payload["diagnostics"] = diagnostics
        return payload
    payload = {
        "resolved": False,
        "reason": resolution.get("reason") or "outcome_not_available",
    }
    if include_diagnostics:
        payload["diagnostics"] = diagnostics
    return payload


def trade_outcome_to_result(trade: dict[str, Any], outcome: str) -> str:
    direction = str(trade.get("direction") or "").strip().lower()
    normalized = str(outcome or "").strip().lower()
    if direction in {"up", "yes", "higher"} and normalized in {"up", "yes", "higher"}:
        return "won"
    if direction in {"down", "no", "lower"} and normalized in {"down", "no", "lower"}:
        return "won"
    return "lost"


def compute_paper_trade_pnl(
    *,
    entry_price: float,
    size: float,
    fee: float = 0.0,
    won: bool,
    action: str = "BUY",
) -> dict[str, float | None]:
    action = str(action or "BUY").upper()
    if action == "BUY":
        paper_cost = float(entry_price) * float(size)
    else:
        paper_cost = max(0.0, (1.0 - float(entry_price)) * float(size))
    payout = float(size) if won else 0.0
    pnl = payout - paper_cost - float(fee or 0.0)
    roi = pnl / paper_cost if paper_cost else None
    return {
        "paper_cost": paper_cost,
        "payout": payout,
        "pnl": pnl,
        "roi": roi,
    }


def _resolve_from_gamma(slug: str) -> dict[str, Any]:
    gamma = resolve_gamma_event_slug(slug)
    market = gamma.get("resolved_market") or {}
    event = gamma.get("resolved_event") or {}
    if not market and not event:
        return {"resolved": False, "reason": "market_not_found"}

    outcome_prices = _outcome_from_prices(market)
    metadata_outcome = _outcome_from_event_metadata(event)
    if outcome_prices and metadata_outcome and not _outcomes_match(outcome_prices, metadata_outcome):
        return {
            "resolved": False,
            "reason": "conflicting_resolution_sources",
            "outcome_prices": outcome_prices,
            "metadata_outcome": metadata_outcome,
        }
    if outcome_prices:
        return {
            "resolved": True,
            "outcome": outcome_prices,
            "source": "gamma_outcome_prices",
            "confidence": 1.0,
            "settlement_timestamp": _settlement_timestamp(market, event),
        }
    if metadata_outcome:
        return {
            "resolved": True,
            "outcome": metadata_outcome,
            "source": "gamma_event_metadata",
            "confidence": 1.0,
            "settlement_timestamp": _settlement_timestamp(market, event),
        }
    return {"resolved": False, "reason": "outcome_not_available"}


def _outcome_from_prices(market: dict[str, Any]) -> str | None:
    if str(market.get("umaResolutionStatus") or "").lower() != "resolved":
        return None
    outcomes = [str(item) for item in _json_list(market.get("outcomes"))]
    prices = _json_list(market.get("outcomePrices") or market.get("outcome_prices"))
    if not outcomes or not prices or len(outcomes) != len(prices):
        return None
    winners = [outcomes[idx] for idx, price in enumerate(prices) if _is_winning_price(price)]
    if len(winners) != 1:
        return None
    return winners[0]


def _outcome_from_event_metadata(event: dict[str, Any]) -> str | None:
    if not event.get("automaticallyResolved") and not event.get("closed"):
        return None
    metadata = event.get("eventMetadata")
    if not isinstance(metadata, dict):
        return None
    final_price = _float_or_none(metadata.get("finalPrice"))
    price_to_beat = _float_or_none(metadata.get("priceToBeat"))
    if final_price is None or price_to_beat is None:
        return None
    return "Up" if final_price >= price_to_beat else "Down"


def _is_winning_price(value: Any) -> bool:
    try:
        return float(value) >= 0.99
    except (TypeError, ValueError):
        return False


def _outcomes_match(left: str, right: str) -> bool:
    return str(left).strip().lower() == str(right).strip().lower()


def _settlement_timestamp(market: dict[str, Any], event: dict[str, Any]) -> str | None:
    for container in (market, event):
        for key in ("closedTime", "closed_time", "endDate", "end_date"):
            value = container.get(key)
            if value:
                return str(value)
    return None


def _probe_settlement_sources(slug: str) -> list[dict[str, Any]]:
    diagnostics: list[dict[str, Any]] = []
    gamma = resolve_gamma_event_slug(slug)
    market = gamma.get("resolved_market") or {}
    event = gamma.get("resolved_event") or {}
    condition_id = _market_condition_id(market)

    diagnostics.append(
        {
            "source": "gamma_events_slug",
            "endpoint": f"{DEFAULT_GAMMA_HOST}/events?{urlencode({'slug': slug})}",
            "found": bool(event),
            "resolved": bool(event.get("automaticallyResolved") or event.get("closed")),
            "payload": _compact_gamma_event(event),
        }
    )
    diagnostics.append(
        {
            "source": "gamma_markets_slug",
            "endpoint": f"{DEFAULT_GAMMA_HOST}/markets?{urlencode({'slug': slug})}",
            "found": bool(market),
            "resolved": str(market.get("umaResolutionStatus") or "").lower() == "resolved",
            "payload": _compact_gamma_market(market),
        }
    )
    diagnostics.append(
        {
            "source": "gamma_outcome_prices",
            "endpoint": "derived_from_gamma_market",
            "found": bool(market),
            "resolved": _outcome_from_prices(market) is not None,
            "payload": {
                "outcomes": market.get("outcomes"),
                "outcomePrices": market.get("outcomePrices") or market.get("outcome_prices"),
                "winning_outcome": _outcome_from_prices(market),
            },
        }
    )
    diagnostics.append(
        {
            "source": "gamma_event_metadata",
            "endpoint": "derived_from_gamma_event",
            "found": bool(event.get("eventMetadata")),
            "resolved": _outcome_from_event_metadata(event) is not None,
            "payload": {
                "eventMetadata": event.get("eventMetadata"),
                "winning_outcome": _outcome_from_event_metadata(event),
            },
        }
    )
    if condition_id:
        diagnostics.append(
            {
                "source": "clob_market",
                "endpoint": f"{DEFAULT_CLOB_HOST}/markets/{condition_id}",
                "found": False,
                "resolved": False,
                "payload": _probe_clob_market(condition_id),
            }
        )
    return diagnostics


def _probe_clob_market(condition_id: str) -> dict[str, Any]:
    try:
        payload = _get_json(f"{DEFAULT_CLOB_HOST}/markets/{condition_id}")
    except Exception as exc:
        return {"error": str(exc)}
    if not isinstance(payload, dict):
        return {"error": "unexpected_payload"}
    return {
        "closed": payload.get("closed"),
        "active": payload.get("active"),
        "tokens": payload.get("tokens"),
        "winner": payload.get("winner"),
        "resolved": payload.get("winner") is not None,
    }


def _compact_gamma_market(market: dict[str, Any]) -> dict[str, Any]:
    if not market:
        return {}
    return {
        "id": market.get("id"),
        "slug": market.get("slug"),
        "closed": market.get("closed"),
        "umaResolutionStatus": market.get("umaResolutionStatus"),
        "outcomes": market.get("outcomes"),
        "outcomePrices": market.get("outcomePrices") or market.get("outcome_prices"),
        "closedTime": market.get("closedTime") or market.get("closed_time"),
        "resolutionSource": market.get("resolutionSource") or market.get("resolution_source"),
    }


def _compact_gamma_event(event: dict[str, Any]) -> dict[str, Any]:
    if not event:
        return {}
    return {
        "id": event.get("id"),
        "slug": event.get("slug"),
        "closed": event.get("closed"),
        "automaticallyResolved": event.get("automaticallyResolved"),
        "eventMetadata": event.get("eventMetadata"),
        "closedTime": event.get("closedTime"),
        "resolutionSource": event.get("resolutionSource") or event.get("resolution_source"),
    }


def _float_or_none(value: Any) -> float | None:
    try:
        return None if value is None or value == "" else float(value)
    except (TypeError, ValueError):
        return None


def settlement_audit(db_path: str) -> dict[str, Any]:
    from src.analysis.short_crypto_paper import ShortCryptoPaperStore, _parse_ts
    import time

    store = ShortCryptoPaperStore(db_path)
    now_ts = time.time()
    unsettled_statuses = {"open", "unknown", "expired_unsettled", "canceled_future_market"}
    with store.connect() as conn:
        trades = [dict(row) for row in conn.execute("SELECT * FROM paper_trades").fetchall()]
    candidates = [
        trade
        for trade in trades
        if _parse_ts(trade["end_time"]) <= now_ts and str(trade.get("status") or "") in unsettled_statuses
    ]

    resolved = 0
    remaining = 0
    sources_tested: list[dict[str, Any]] = []
    samples: list[dict[str, Any]] = []

    for trade in candidates:
        slug = str(trade.get("slug") or "")
        if trade.get("venue") != "polymarket" or not slug:
            remaining += 1
            continue
        probe = resolve_polymarket_short_crypto_market(slug, include_diagnostics=True)
        if probe.get("resolved"):
            resolved += 1
        else:
            remaining += 1
        if len(samples) < 5:
            samples.append(
                {
                    "trade_id": trade.get("id"),
                    "slug": slug,
                    "status": trade.get("status"),
                    "resolution": probe,
                }
            )
        seen_sources = {item.get("source") for item in sources_tested}
        for item in probe.get("diagnostics") or []:
            source_name = item.get("source")
            if source_name not in seen_sources:
                sources_tested.append(item)
                seen_sources.add(source_name)

    return {
        "expired_unsettled": len(candidates),
        "resolved": resolved,
        "remaining": remaining,
        "sources_tested": sources_tested,
        "samples": samples,
        "audited_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
    }
