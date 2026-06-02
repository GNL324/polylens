from __future__ import annotations

from collections import defaultdict
from typing import Any


def match_prop_pairs(props: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    result = match_prop_pairs_with_metrics(props)
    return result["matches"], result["rejects"]


def match_prop_pairs_with_metrics(props: list[dict[str, Any]]) -> dict[str, Any]:
    groups: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
    invalid_side_rejects: list[dict[str, Any]] = []
    for prop in props:
        side = prop.get("side")
        if side not in {"over", "under"}:
            invalid_side_rejects.append(_reject(prop, "invalid side"))
            continue
        groups[_group_key(prop)].append(prop)

    matches: list[dict[str, Any]] = []
    rejects: list[dict[str, Any]] = list(invalid_side_rejects)
    comparisons_after = 0
    for key, rows in groups.items():
        overs = [row for row in rows if row.get("side") == "over"]
        unders = [row for row in rows if row.get("side") == "under"]
        if not overs or not unders:
            sample = rows[0] if rows else {}
            rejects.append(_reject(sample, "missing opposite side"))
            continue
        for over in overs:
            for under in unders:
                comparisons_after += 1
                matches.append({"over": over, "under": under, "player": over.get("player"), "market_type": over.get("market_type"), "line": over.get("line"), "event_id": over.get("event_id")})

    before = len(props) * (len(props) - 1) // 2
    reduction = round(((before - comparisons_after) / before) * 100, 4) if before else 0.0
    return {
        "matches": matches,
        "rejects": rejects,
        "metrics": {
            "comparisons_before_grouping": before,
            "comparisons_after_grouping": comparisons_after,
            "grouping_reduction_percent": reduction,
        },
    }


def prop_rejection_reason(left: dict[str, Any], right: dict[str, Any]) -> str | None:
    if _group_key(left) != _group_key(right):
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
        return "invalid side"
    return None


def _group_key(prop: dict[str, Any]) -> tuple[Any, ...]:
    return (
        prop.get("sport"),
        prop.get("league"),
        prop.get("event_id"),
        _norm(prop.get("player")),
        prop.get("market_type"),
        _line(prop.get("line")),
    )


def _reject(prop: dict[str, Any], reason: str) -> dict[str, Any]:
    return {
        "player": prop.get("player"),
        "market_type": prop.get("market_type"),
        "line": prop.get("line"),
        "event_id": prop.get("event_id"),
        "rejection_reason": reason,
    }


def _norm(value: Any) -> str:
    return " ".join(str(value or "").lower().split())


def _line(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
