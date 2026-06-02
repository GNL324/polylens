from __future__ import annotations

from collections import defaultdict
from typing import Any


def dedupe_best_outcomes(outcomes: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    books = {str(row.get("bookmaker_name") or row.get("bookmaker") or "unknown") for row in outcomes}
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in outcomes:
        if row.get("team"):
            grouped[_norm(str(row.get("team")))].append(row)
    best_rows: list[dict[str, Any]] = []
    best_sources: dict[str, dict[str, Any]] = {}
    for team, rows in grouped.items():
        best = min(rows, key=lambda row: _num(row.get("implied_probability") or row.get("price")) if _num(row.get("implied_probability") or row.get("price")) is not None else 999)
        best_rows.append(best)
        best_sources[str(best.get("team"))] = {
            "bookmaker": best.get("bookmaker_name") or best.get("bookmaker"),
            "odds": best.get("odds"),
            "implied_probability": _num(best.get("implied_probability") or best.get("price")),
        }
    diagnostics = {
        "futures_retained_outcomes": len([row for row in outcomes if row.get("team")]),
        "unique_outcomes": len(grouped),
        "duplicate_outcomes_removed": max(0, len([row for row in outcomes if row.get("team")]) - len(grouped)),
        "books_considered": sorted(books),
        "best_price_source": best_sources,
    }
    return best_rows, diagnostics


def build_synthetic_field(outcomes: list[dict[str, Any]], selected_team: str, expected_teams: list[str] | None = None) -> dict[str, Any]:
    selected = _norm(selected_team)
    unique_rows, diagnostics = dedupe_best_outcomes(outcomes)
    field_rows = [row for row in unique_rows if _norm(str(row.get("team"))) != selected]
    missing = _missing_expected(unique_rows, expected_teams or [])
    complete = bool(field_rows) and not missing and _has_selected(unique_rows, selected)
    if not complete:
        return {
            "field_available": bool(field_rows),
            "coverage_complete": False,
            "coverage_percent": _coverage_percent(unique_rows, expected_teams),
            "field_price": None,
            "field_outcomes": field_rows,
            "diagnostic": "missing team outcomes" if missing else "incomplete field",
            "missing_teams": missing,
            **diagnostics,
        }
    field_price = _best_field_price(field_rows)
    return {
        "field_available": True,
        "coverage_complete": field_price is not None,
        "coverage_percent": 100 if field_price is not None else 65,
        "field_price": field_price,
        "field_outcomes": field_rows,
        "diagnostic": "full hedge" if field_price is not None else "incomplete field",
        "missing_teams": [],
        **diagnostics,
    }


def debug_synthetic_field(outcomes: list[dict[str, Any]], selected_team: str, expected_teams: list[str] | None = None) -> dict[str, Any]:
    field = build_synthetic_field(outcomes, selected_team, expected_teams=expected_teams)
    selected_row = next((row for row in dedupe_best_outcomes(outcomes)[0] if _norm(str(row.get("team"))) == _norm(selected_team)), None)
    selected_price = _num((selected_row or {}).get("implied_probability") or (selected_row or {}).get("price"))
    implied_sum = round((selected_price or 0) + (field.get("field_price") or 0), 6) if selected_price is not None and field.get("field_price") is not None else None
    return {
        "selected_team": selected_team,
        "selected_price": selected_price,
        "field_members": [row.get("team") for row in field.get("field_outcomes", [])],
        "best_book_per_outcome": field.get("best_price_source", {}),
        "implied_probability_sum": implied_sum,
        **field,
    }


def _best_field_price(rows: list[dict[str, Any]]) -> float | None:
    prices = [_num(row.get("implied_probability") or row.get("price")) for row in rows]
    prices = [price for price in prices if price is not None]
    if not prices:
        return None
    return round(sum(prices), 6)


def _coverage_percent(rows: list[dict[str, Any]], expected: list[str] | None) -> int:
    if not expected:
        return 65 if rows else 0
    present = {_norm(str(row.get("team"))) for row in rows}
    return int(100 * len(present & {_norm(team) for team in expected}) / len(expected))


def _missing_expected(rows: list[dict[str, Any]], expected: list[str]) -> list[str]:
    present = {_norm(str(row.get("team"))) for row in rows}
    return [team for team in expected if _norm(team) not in present]


def _has_selected(rows: list[dict[str, Any]], selected: str) -> bool:
    return any(_norm(str(row.get("team"))) == selected for row in rows)


def _norm(value: str) -> str:
    return " ".join(value.lower().split())


def _num(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
