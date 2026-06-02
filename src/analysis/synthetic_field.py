from __future__ import annotations

from typing import Any


def build_synthetic_field(outcomes: list[dict[str, Any]], selected_team: str, expected_teams: list[str] | None = None) -> dict[str, Any]:
    selected = _norm(selected_team)
    rows = [row for row in outcomes if row.get("team")]
    field_rows = [row for row in rows if _norm(str(row.get("team"))) != selected]
    missing = _missing_expected(rows, expected_teams or [])
    complete = bool(field_rows) and not missing and _has_selected(rows, selected)
    if not complete:
        return {
            "field_available": bool(field_rows),
            "coverage_complete": False,
            "coverage_percent": _coverage_percent(rows, expected_teams),
            "field_price": None,
            "field_outcomes": field_rows,
            "diagnostic": "missing team outcomes" if missing else "incomplete field",
            "missing_teams": missing,
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
    }


def _best_field_price(rows: list[dict[str, Any]]) -> float | None:
    prices = [_num(row.get("implied_probability") or row.get("price")) for row in rows]
    prices = [price for price in prices if price is not None]
    if not prices:
        return None
    # A synthetic field made from many mutually exclusive outcomes costs the sum of its legs.
    return round(sum(prices), 6)


def _coverage_percent(rows: list[dict[str, Any]], expected: list[str] | None) -> int:
    if not expected:
        return 65 if rows else 0
    if not expected:
        return 0
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
