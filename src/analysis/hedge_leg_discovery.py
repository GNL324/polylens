from __future__ import annotations

from typing import Any

FIELD_TERMS = ("field", "any other", "any other team", "not ", "no ", "does not", "will not")


def discover_hedge_leg(candidate: dict[str, Any], inventory: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    inventory = inventory or []
    direct = _direct_coverage(candidate)
    if direct["hedge_leg_found"]:
        return direct

    target_team = _team(candidate)
    target_market = str(candidate.get("market_type") or "")
    source_title = _title(candidate)
    best_partial = 0
    settlement_mismatch = False
    for market in inventory:
        text = _market_text(market)
        if target_team and target_team not in text:
            if _is_field_contract(text, target_team):
                result = _inventory_result(market, 100, "field_complete")
                if _settlement_mismatch(source_title, text):
                    result.update({"hedge_leg_found": False, "coverage_percent": 65, "hedge_type": "partial", "diagnostic": "settlement mismatch"})
                return result
            continue
        if _is_no_contract(text, target_team) and _market_type_compatible(target_market, text):
            result = _inventory_result(market, 100, "binary_complete")
            if _settlement_mismatch(source_title, text):
                result.update({"hedge_leg_found": False, "coverage_percent": 65, "hedge_type": "partial", "diagnostic": "settlement mismatch"})
            return result
        if target_team and target_team in text:
            if _settlement_mismatch(source_title, text):
                settlement_mismatch = True
            best_partial = max(best_partial, 65)
    if settlement_mismatch:
        return _missing("settlement mismatch", coverage=best_partial or 65)
    if best_partial:
        return _missing("partial hedge", coverage=best_partial)
    return _missing("missing hedge leg")


def explain_hedge_search(candidate: dict[str, Any], inventory: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    result = discover_hedge_leg(candidate, inventory)
    return {
        "candidate_title": _title(candidate),
        "team": _team(candidate),
        "hedge_leg_found": result["hedge_leg_found"],
        "coverage_percent": result["coverage_percent"],
        "hedge_type": result["hedge_type"],
        "diagnostic": result.get("diagnostic"),
        "hedge_leg": result.get("hedge_leg"),
    }


def _direct_coverage(candidate: dict[str, Any]) -> dict[str, Any]:
    has_yes = any(_num(candidate.get(key)) is not None for key in ("polymarket_implied_yes_price", "polymarket_yes_price", "kalshi_implied_yes_price", "kalshi_yes_price", "sportsbook_implied_probability"))
    has_no = any(_num(candidate.get(key)) is not None for key in ("polymarket_implied_no_price", "polymarket_no_price", "kalshi_implied_no_price", "kalshi_no_price"))
    if has_yes and has_no:
        return {"hedge_leg_found": True, "coverage_percent": 100, "hedge_type": "binary_complete", "diagnostic": "full hedge"}
    return {"hedge_leg_found": False, "coverage_percent": 0, "hedge_type": "none", "diagnostic": "missing hedge leg"}


def _inventory_result(market: dict[str, Any], coverage: int, hedge_type: str) -> dict[str, Any]:
    return {"hedge_leg_found": coverage == 100, "coverage_percent": coverage, "hedge_type": hedge_type, "diagnostic": "full hedge" if coverage == 100 else "partial hedge", "hedge_leg": market}


def _missing(reason: str, coverage: int = 0) -> dict[str, Any]:
    return {"hedge_leg_found": False, "coverage_percent": coverage, "hedge_type": "partial" if coverage else "none", "diagnostic": reason}


def _is_no_contract(text: str, team: str | None) -> bool:
    if not team:
        return " no " in text or " will not " in text or " not " in text
    return f"no {team}" in text or f"not {team}" in text or f"{team} no" in text or " will not " in text or " does not " in text


def _is_field_contract(text: str, team: str | None) -> bool:
    if "field" in text or "any other" in text:
        return True
    return bool(team and f"not {team}" in text)


def _market_type_compatible(market_type: str, text: str) -> bool:
    if market_type == "championship_winner":
        return any(term in text for term in ("champion", "championship", "finals", "winner", "title"))
    if market_type == "game_winner":
        return any(term in text for term in ("beat", "winner", "moneyline", "win"))
    return True


def _settlement_mismatch(source: str, target: str) -> bool:
    source_has_series = any(term in source for term in ("finals", "championship", "season"))
    target_has_game = any(term in target for term in ("game", "tonight", " vs ", " at "))
    return source_has_series and target_has_game


def _team(candidate: dict[str, Any]) -> str | None:
    value = candidate.get("sportsbook_team") or candidate.get("team")
    if value:
        return str(value).lower()
    title = _title(candidate)
    for prefix in ("will the ", "will "):
        if title.startswith(prefix):
            return title.removeprefix(prefix).split(" win ")[0].strip() or None
    return None


def _title(candidate: dict[str, Any]) -> str:
    return str(candidate.get("polymarket_title") or candidate.get("kalshi_title") or candidate.get("title") or "").lower()


def _market_text(market: dict[str, Any]) -> str:
    fields = ("title", "question", "subtitle", "outcome", "ticker", "market_type")
    return " ".join(str(market.get(field) or "") for field in fields).lower()


def _num(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
