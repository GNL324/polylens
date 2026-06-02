from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from math import log10
from typing import Any


def score_candidates(candidates: list[dict[str, Any]], now: datetime | None = None) -> list[dict[str, Any]]:
    scored = [score_candidate(candidate, now=now) for candidate in candidates]
    scored.sort(key=lambda item: item.get("execution_score") or 0, reverse=True)
    return scored


def score_candidate(candidate: dict[str, Any], now: datetime | None = None) -> dict[str, Any]:
    now = _now(now)
    raw_edge = _number(candidate.get("estimated_edge"))
    liquidity_score = _liquidity_score(candidate)
    confidence_score = _confidence_score(candidate)
    freshness_score = _freshness_score(candidate, now)
    time_score = _time_score(candidate, now)
    edge_score = _edge_score(raw_edge)
    missing_penalty = _missing_data_penalty(candidate)
    stale_penalty = 0.18 if freshness_score < 0.45 else 0.0
    low_liquidity_penalty = 0.12 if liquidity_score < 0.35 else 0.0
    soon_close_penalty = 0.12 if time_score < 0.35 else 0.0

    execution = (
        0.35 * edge_score
        + 0.20 * confidence_score
        + 0.15 * liquidity_score
        + 0.15 * freshness_score
        + 0.15 * time_score
        - missing_penalty
        - stale_penalty
        - low_liquidity_penalty
        - soon_close_penalty
    )
    result = dict(candidate)
    result.update({
        "raw_edge": raw_edge,
        "estimated_edge": raw_edge,
        "liquidity_score": round(liquidity_score, 4),
        "confidence_score": round(confidence_score, 4),
        "freshness_score": round(freshness_score, 4),
        "time_score": round(time_score, 4),
        "execution_score": round(_clamp(execution), 4),
        "score_reason": _score_reason(raw_edge, liquidity_score, confidence_score, freshness_score, time_score, missing_penalty, stale_penalty, low_liquidity_penalty, soon_close_penalty),
    })
    return result


def filter_scored_candidates(
    candidates: list[dict[str, Any]],
    min_edge: float | None = None,
    min_score: float | None = None,
    max_close_hours: float | None = None,
    include_low_confidence: bool = False,
    now: datetime | None = None,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    now = _now(now)
    reasons: Counter[str] = Counter()
    filtered: list[dict[str, Any]] = []
    for candidate in candidates:
        edge = _number(candidate.get("estimated_edge"))
        score = _number(candidate.get("execution_score")) or 0.0
        confidence = _number(candidate.get("confidence_score")) or 0.0
        close_hours = _hours_to_close(candidate, now)
        rejected = False
        if min_edge is not None and (edge is None or edge < min_edge):
            reasons["below min edge"] += 1
            rejected = True
        if min_score is not None and score < min_score:
            reasons["below min execution score"] += 1
            rejected = True
        if max_close_hours is not None and close_hours is not None and close_hours > max_close_hours:
            reasons["outside max close window"] += 1
            rejected = True
        if not include_low_confidence and confidence < 0.55:
            reasons["low confidence excluded"] += 1
            rejected = True
        if not rejected:
            filtered.append(candidate)
    filtered.sort(key=lambda item: item.get("execution_score") or 0, reverse=True)
    return filtered, dict(reasons)


def _edge_score(edge: float | None) -> float:
    if edge is None or edge <= 0:
        return 0.0
    return _clamp(edge / 0.12)


def _liquidity_score(candidate: dict[str, Any]) -> float:
    value = _first_number(candidate, ("liquidity", "kalshi_liquidity", "polymarket_liquidity", "sportsbook_liquidity"))
    if value is None:
        warning = str(candidate.get("liquidity_warning") or "").lower()
        return 0.2 if "zero" in warning else 0.5
    if value <= 0:
        return 0.15
    return _clamp((log10(value + 1) / 5) + 0.2)


def _confidence_score(candidate: dict[str, Any]) -> float:
    explicit = _number(candidate.get("confidence_score"))
    if explicit is not None:
        return _clamp(explicit)
    similarity = _number(candidate.get("similarity_score"))
    if similarity is not None:
        return _clamp(similarity)
    band = str(candidate.get("confidence_band") or "").lower()
    return {"high": 0.9, "medium": 0.72, "low": 0.45}.get(band, 0.55)


def _freshness_score(candidate: dict[str, Any], now: datetime) -> float:
    timestamp = _first_timestamp(candidate, ("last_update", "updated_at", "updatedAt", "latest_trade_timestamp", "createdAt"))
    if timestamp is None:
        return 0.7
    hours = max(0.0, (now - timestamp).total_seconds() / 3600)
    if hours <= 1:
        return 1.0
    if hours <= 6:
        return 0.82
    if hours <= 24:
        return 0.58
    if hours <= 72:
        return 0.35
    return 0.18


def _time_score(candidate: dict[str, Any], now: datetime) -> float:
    hours = _hours_to_close(candidate, now)
    if hours is None:
        return 0.7
    if hours <= 0:
        return 0.05
    if hours <= 1:
        return 0.22
    if hours <= 6:
        return 0.48
    if hours <= 24:
        return 0.76
    return 0.9


def _missing_data_penalty(candidate: dict[str, Any]) -> float:
    penalty = 0.0
    if candidate.get("price_data_missing") or candidate.get("pricing_status") == "insufficient pricing data" or candidate.get("estimated_edge") is None:
        penalty += 0.2
    if _first_number(candidate, ("liquidity", "kalshi_liquidity", "polymarket_liquidity", "sportsbook_liquidity")) is None:
        penalty += 0.06
    if _first_timestamp(candidate, ("last_update", "updated_at", "updatedAt", "latest_trade_timestamp", "createdAt")) is None:
        penalty += 0.04
    return penalty


def _score_reason(edge: float | None, liquidity: float, confidence: float, freshness: float, time: float, missing: float, stale: float, low_liq: float, soon: float) -> str:
    parts = []
    parts.append("edge available" if edge is not None and edge > 0 else "no positive edge")
    parts.append(f"confidence={confidence:.2f}")
    parts.append(f"liquidity={liquidity:.2f}")
    parts.append(f"freshness={freshness:.2f}")
    parts.append(f"time={time:.2f}")
    penalties = []
    if missing:
        penalties.append("missing data")
    if stale:
        penalties.append("stale price")
    if low_liq:
        penalties.append("low liquidity")
    if soon:
        penalties.append("soon to close")
    if penalties:
        parts.append("penalties: " + ", ".join(penalties))
    return "; ".join(parts)


def _hours_to_close(candidate: dict[str, Any], now: datetime) -> float | None:
    close = _first_timestamp(candidate, ("close_time", "close_or_commence_time", "commence_time", "expiration_time", "end_date", "endDate"))
    if close is None:
        return None
    return (close - now).total_seconds() / 3600


def _first_number(candidate: dict[str, Any], keys: tuple[str, ...]) -> float | None:
    for key in keys:
        value = _number(candidate.get(key))
        if value is not None:
            return value
    return None


def _first_timestamp(candidate: dict[str, Any], keys: tuple[str, ...]) -> datetime | None:
    for key in keys:
        value = candidate.get(key)
        parsed = _parse_timestamp(value)
        if parsed is not None:
            return parsed
    structured = candidate.get("structured_match") if isinstance(candidate.get("structured_match"), dict) else {}
    for source in structured.values():
        if isinstance(source, dict):
            for key in keys:
                parsed = _parse_timestamp(source.get(key))
                if parsed is not None:
                    return parsed
    return None


def _parse_timestamp(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        numeric = float(value)
        if numeric <= 0:
            return None
        if numeric > 10_000_000_000:
            numeric = numeric / 1000
        return datetime.fromtimestamp(numeric, tz=timezone.utc)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
        except ValueError:
            return None
    return None


def _number(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    return numeric


def _now(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))
