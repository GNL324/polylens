from __future__ import annotations

from collections import Counter
from typing import Any


def summarize_futures_inventory(raw_events: list[dict[str, Any]], normalized: list[dict[str, Any]], retained: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    retained = retained if retained is not None else normalized
    raw_outcomes = _raw_outcome_rows(raw_events)
    normalized_names = [str(row.get("team")) for row in normalized if row.get("team")]
    retained_names = [str(row.get("team")) for row in retained if row.get("team")]
    market_summaries = []
    for event in raw_events:
        for bookmaker in event.get("bookmakers", []) or []:
            for market in bookmaker.get("markets", []) or []:
                outcomes = market.get("outcomes", []) or []
                market_summaries.append({
                    "sport_key": event.get("sport_key"),
                    "sport_title": event.get("sport_title") or event.get("title"),
                    "bookmaker": bookmaker.get("title") or bookmaker.get("key"),
                    "bookmaker_key": bookmaker.get("key"),
                    "market_key": market.get("key"),
                    "outcome_count": len(outcomes),
                    "outcome_names": [outcome.get("name") for outcome in outcomes],
                })
    return {
        "futures_raw_outcomes": len(raw_outcomes),
        "futures_normalized_outcomes": len(normalized),
        "futures_retained_outcomes": len(retained),
        "raw_unique_outcomes": sorted({str(row.get("name")) for row in raw_outcomes if row.get("name")}),
        "normalized_unique_outcomes": sorted(set(normalized_names)),
        "retained_unique_outcomes": sorted(set(retained_names)),
        "discard_counts": {
            "raw_to_normalized": max(0, len(raw_outcomes) - len(normalized)),
            "normalized_to_retained": max(0, len(normalized) - len(retained)),
        },
        "bookmaker_counts": dict(Counter(row.get("bookmaker") for row in market_summaries)),
        "markets": market_summaries,
    }


def _raw_outcome_rows(raw_events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for event in raw_events:
        for bookmaker in event.get("bookmakers", []) or []:
            for market in bookmaker.get("markets", []) or []:
                for outcome in market.get("outcomes", []) or []:
                    rows.append({"name": outcome.get("name"), "bookmaker": bookmaker.get("title") or bookmaker.get("key"), "market_key": market.get("key"), "sport_key": event.get("sport_key")})
    return rows
