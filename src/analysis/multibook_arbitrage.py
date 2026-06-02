from __future__ import annotations

from collections import defaultdict
from typing import Any

from src.analysis.synthetic_field import build_synthetic_field


def scan_multibook_arbitrage(lines: list[dict[str, Any]], bankroll: float | None = None, min_guaranteed_roi: float | None = None) -> dict[str, Any]:
    moneyline_arbs: list[dict[str, Any]] = []
    field_arbs: list[dict[str, Any]] = []
    incomplete: list[dict[str, Any]] = []
    diagnostics: dict[str, int] = defaultdict(int)

    for group in _group_game_lines(lines).values():
        result = _scan_two_way_group(group, bankroll)
        if result and result.get("opportunity_type") == "true_arbitrage":
            moneyline_arbs.append(result)
        elif result:
            diagnostics[result.get("diagnostic", "unknown")] += 1

    for group in _group_futures(lines).values():
        arbs, rejects = _scan_futures_group(group, bankroll)
        field_arbs.extend(arbs)
        incomplete.extend(rejects)
        for reject in rejects:
            diagnostics[reject.get("diagnostic", "unknown")] += 1

    if min_guaranteed_roi is not None:
        moneyline_arbs = [item for item in moneyline_arbs if (item.get("guaranteed_roi") or 0) >= min_guaranteed_roi]
        field_arbs = [item for item in field_arbs if (item.get("guaranteed_roi") or 0) >= min_guaranteed_roi]

    moneyline_arbs.sort(key=lambda item: item.get("guaranteed_roi") or 0, reverse=True)
    field_arbs.sort(key=lambda item: item.get("guaranteed_roi") or 0, reverse=True)
    return {
        "sportsbook_multibook_arbs": moneyline_arbs,
        "synthetic_field_arbs": field_arbs,
        "incomplete_hedges": incomplete,
        "diagnostics": dict(diagnostics),
    }


def _scan_two_way_group(lines: list[dict[str, Any]], bankroll: float | None) -> dict[str, Any] | None:
    teams = sorted({str(line.get("team")) for line in lines if line.get("team")})
    if len(teams) < 2:
        return {"diagnostic": "no opposite side found", "lines": lines}
    if len(teams) > 2:
        return None
    best = []
    for team in teams:
        team_lines = [line for line in lines if str(line.get("team")) == team]
        best_line = min(team_lines, key=lambda line: _prob(line) or 999)
        best.append(best_line)
    total = round(sum(_prob(line) or 999 for line in best), 6)
    if total >= 1:
        return {"diagnostic": "total implied probability >= 1", "implied_probability_sum": total, "lines": best}
    return _arb_payload(best[0], best[1], total, bankroll, "sportsbook_two_way")


def _scan_futures_group(lines: list[dict[str, Any]], bankroll: float | None) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    arbs: list[dict[str, Any]] = []
    rejects: list[dict[str, Any]] = []
    teams = sorted({str(line.get("team")) for line in lines if line.get("team")})
    for selected in teams:
        selected_lines = [line for line in lines if str(line.get("team")) == selected]
        selected_line = min(selected_lines, key=lambda line: _prob(line) or 999)
        field = build_synthetic_field(lines, selected, expected_teams=teams)
        if not field["coverage_complete"]:
            rejects.append({"team": selected, "diagnostic": field.get("diagnostic") or "incomplete field", "coverage_percent": field.get("coverage_percent"), "missing_teams": field.get("missing_teams")})
            continue
        total = round((_prob(selected_line) or 999) + (field.get("field_price") or 999), 6)
        if total >= 1:
            rejects.append({"team": selected, "diagnostic": "total implied probability >= 1", "implied_probability_sum": total})
            continue
        arbs.append(_arb_payload(selected_line, {"team": "Field", "bookmaker_name": "synthetic_field", "implied_probability": field["field_price"], "field_outcomes": field["field_outcomes"]}, total, bankroll, "synthetic_field"))
    return arbs, rejects


def _arb_payload(line_a: dict[str, Any], line_b: dict[str, Any], total: float, bankroll: float | None, arb_type: str) -> dict[str, Any]:
    roi = round((1 - total) / total, 6) if total > 0 else None
    payload = {
        "opportunity_type": "true_arbitrage",
        "arb_type": arb_type,
        "leg_a": _leg(line_a),
        "leg_b": _leg(line_b),
        "implied_probability_sum": total,
        "guaranteed_profit": round(1 - total, 6),
        "guaranteed_roi": roi,
    }
    if bankroll is not None:
        payload.update(_stake_size(_prob(line_a) or 0, _prob(line_b) or 0, bankroll, total))
    return payload


def _stake_size(prob_a: float, prob_b: float, bankroll: float, total: float) -> dict[str, Any]:
    if total <= 0:
        return {"stake_a": 0.0, "stake_b": 0.0, "guaranteed_profit_amount": 0.0}
    return {
        "stake_a": round(float(bankroll) * prob_a / total, 2),
        "stake_b": round(float(bankroll) * prob_b / total, 2),
        "guaranteed_profit_amount": round(float(bankroll) * ((1 - total) / total), 2),
    }


def _leg(line: dict[str, Any]) -> dict[str, Any]:
    return {"bookmaker": line.get("bookmaker_name"), "team": line.get("team"), "odds": line.get("odds"), "implied_probability": _prob(line)}


def _group_game_lines(lines: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for line in lines:
        if line.get("market_type") != "game_winner":
            continue
        key = str(line.get("event_id") or "")
        if key:
            groups[key].append(line)
    return groups


def _group_futures(lines: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for line in lines:
        if line.get("market_type") != "championship_winner":
            continue
        key = f"{line.get('league')}:{line.get('event_id') or line.get('market_title') or 'futures'}"
        groups[key].append(line)
    return groups


def _prob(line: dict[str, Any]) -> float | None:
    value = line.get("implied_probability") or line.get("price")
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
