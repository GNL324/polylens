from __future__ import annotations

from typing import Any


def match_prop_pairs(props: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    matches: list[dict[str, Any]] = []
    rejects: list[dict[str, Any]] = []
    for index, left in enumerate(props):
        for right in props[index + 1:]:
            reason = prop_rejection_reason(left, right)
            if reason:
                rejects.append({"left": left, "right": right, "rejection_reason": reason})
                continue
            over = left if left.get("side") == "over" else right
            under = right if left.get("side") == "over" else left
            matches.append({"over": over, "under": under, "player": over.get("player"), "market_type": over.get("market_type"), "line": over.get("line"), "event_id": over.get("event_id")})
    return matches, rejects


def prop_rejection_reason(left: dict[str, Any], right: dict[str, Any]) -> str | None:
    if left.get("sport") != right.get("sport") or left.get("league") != right.get("league") or left.get("event_id") != right.get("event_id"):
        return "event mismatch"
    if _norm(left.get("player")) != _norm(right.get("player")):
        return "player mismatch"
    if left.get("market_type") != right.get("market_type"):
        return "market mismatch"
    if _line(left.get("line")) != _line(right.get("line")):
        return "line mismatch"
    if left.get("side") == right.get("side"):
        return "same side"
    if left.get("side") not in {"over", "under"} or right.get("side") not in {"over", "under"}:
        return "same side"
    return None


def _norm(value: Any) -> str:
    return " ".join(str(value or "").lower().split())


def _line(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
