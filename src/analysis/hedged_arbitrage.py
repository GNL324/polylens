from __future__ import annotations

from typing import Any

TRUE_ARB_REASONS = {
    "markets not equivalent",
    "missing NO side",
    "no hedge leg",
    "total cost >= 1",
    "settlement rules differ",
    "event scope mismatch",
}


def classify_arbitrage_candidates(candidates: list[dict[str, Any]], bankroll: float | None = None, include_hedges: bool = False) -> dict[str, list[dict[str, Any]]]:
    true_arbs: list[dict[str, Any]] = []
    hedges: list[dict[str, Any]] = []
    positive_ev: list[dict[str, Any]] = []
    for candidate in candidates:
        enriched = classify_candidate(candidate, bankroll=bankroll, include_hedges=include_hedges)
        if enriched.get("opportunity_type") == "true_arbitrage":
            true_arbs.append(enriched)
        elif enriched.get("opportunity_type") == "cross_market_hedge":
            hedges.append(enriched)
        else:
            positive_ev.append(enriched)
    true_arbs.sort(key=lambda item: item.get("guaranteed_roi") or 0, reverse=True)
    hedges.sort(key=lambda item: item.get("execution_score") or item.get("estimated_edge") or 0, reverse=True)
    positive_ev.sort(key=lambda item: item.get("execution_score") or item.get("estimated_edge") or 0, reverse=True)
    return {
        "true_arbitrage_candidates": true_arbs,
        "cross_market_hedge_candidates": hedges,
        "positive_ev_candidates": positive_ev,
    }


def classify_candidate(candidate: dict[str, Any], bankroll: float | None = None, include_hedges: bool = False) -> dict[str, Any]:
    row = dict(candidate)
    true_arb = detect_true_yes_no_arbitrage(row, bankroll=bankroll)
    if true_arb:
        row.update(true_arb)
        return row

    reason = _not_true_arbitrage_reason(row)
    row["true_arbitrage_rejection_reason"] = reason
    if include_hedges:
        hedge = detect_cross_market_hedge(row)
        if hedge:
            row.update(hedge)
            return row
    row.setdefault("opportunity_type", "positive_ev")
    return row


def detect_true_yes_no_arbitrage(candidate: dict[str, Any], bankroll: float | None = None) -> dict[str, Any] | None:
    venue_pair = str(candidate.get("venue_pair") or "")
    pm_yes = _num(candidate.get("polymarket_implied_yes_price") or candidate.get("polymarket_yes_price"))
    pm_no = _num(candidate.get("polymarket_implied_no_price") or candidate.get("polymarket_no_price"))
    kalshi_yes = _num(candidate.get("kalshi_implied_yes_price") or candidate.get("kalshi_yes_price"))
    kalshi_no = _num(candidate.get("kalshi_implied_no_price") or candidate.get("kalshi_no_price"))

    options: list[dict[str, Any]] = []
    if "polymarket_kalshi" in venue_pair or (kalshi_yes is not None or kalshi_no is not None):
        if pm_yes is not None and kalshi_no is not None:
            options.append(_arb_payload("polymarket", "YES", pm_yes, "kalshi", "NO", kalshi_no, bankroll))
        if kalshi_yes is not None and pm_no is not None:
            options.append(_arb_payload("kalshi", "YES", kalshi_yes, "polymarket", "NO", pm_no, bankroll))

    sportsbook_prob = _num(candidate.get("sportsbook_implied_probability"))
    sportsbook_odds = candidate.get("odds")
    market_type = candidate.get("market_type")
    if sportsbook_prob is not None and market_type in {"game_winner", "championship_winner"}:
        if pm_no is not None and _fully_covers_sportsbook_leg(candidate):
            options.append(_arb_payload(str(candidate.get("sportsbook") or "sportsbook"), str(candidate.get("sportsbook_team") or "winner"), sportsbook_prob, "polymarket", "NO", pm_no, bankroll, odds_a=sportsbook_odds))
        if kalshi_no is not None and _fully_covers_sportsbook_leg(candidate):
            options.append(_arb_payload(str(candidate.get("sportsbook") or "sportsbook"), str(candidate.get("sportsbook_team") or "winner"), sportsbook_prob, "kalshi", "NO", kalshi_no, bankroll, odds_a=sportsbook_odds))

    profitable = [item for item in options if item["total_cost"] < 1.0]
    if not profitable:
        return None
    best = min(profitable, key=lambda item: item["total_cost"])
    best.update({"opportunity_type": "true_arbitrage", "confidence": "true_arb"})
    return best


def detect_cross_market_hedge(candidate: dict[str, Any]) -> dict[str, Any] | None:
    if not _has_related_markets(candidate):
        return None
    if candidate.get("confidence_band") == "low":
        return None
    reason = _not_true_arbitrage_reason(candidate)
    if reason in {"missing NO side", "event scope mismatch", "settlement rules differ"}:
        return {
            "opportunity_type": "cross_market_hedge",
            "hedge_type": "cross_market_hedge",
            "residual_risk": _residual_risk(candidate, reason),
        }
    return None


def _arb_payload(venue_a: str, outcome_a: str, price_a: float, venue_b: str, outcome_b: str, price_b: float, bankroll: float | None, odds_a: Any | None = None) -> dict[str, Any]:
    total_cost = round(price_a + price_b, 6)
    guaranteed_profit = round(1.0 - total_cost, 6)
    guaranteed_roi = round(guaranteed_profit / total_cost, 6) if total_cost > 0 else None
    payload = {
        "buy_venue_a": venue_a,
        "buy_outcome_a": outcome_a,
        "price_a": price_a,
        "buy_venue_b": venue_b,
        "buy_outcome_b": outcome_b,
        "price_b": price_b,
        "total_cost": total_cost,
        "guaranteed_profit": guaranteed_profit,
        "guaranteed_roi": guaranteed_roi,
        "stake_ratio": _stake_ratio(price_a, price_b),
    }
    if bankroll is not None:
        payload.update(size_true_arb(price_a, price_b, bankroll))
    if odds_a is not None:
        payload["sportsbook_odds"] = odds_a
    return payload


def size_true_arb(price_a: float, price_b: float, bankroll: float) -> dict[str, Any]:
    total = price_a + price_b
    if total <= 0:
        return {"stake_a": 0.0, "stake_b": 0.0, "guaranteed_profit_amount": 0.0}
    stake_a = float(bankroll) * price_a / total
    stake_b = float(bankroll) * price_b / total
    profit_amount = float(bankroll) * ((1.0 - total) / total)
    return {
        "stake_a": round(stake_a, 2),
        "stake_b": round(stake_b, 2),
        "guaranteed_profit_amount": round(profit_amount, 2),
    }


def _stake_ratio(price_a: float, price_b: float) -> str:
    if price_b == 0:
        return "1:0"
    return f"{round(price_a / price_b, 4)}:1"


def _not_true_arbitrage_reason(candidate: dict[str, Any]) -> str:
    if not _markets_equivalent(candidate):
        return "markets not equivalent"
    if not _has_no_side(candidate):
        return "missing NO side"
    if not _has_hedge_leg(candidate):
        return "no hedge leg"
    if _candidate_min_total_cost(candidate) is not None and (_candidate_min_total_cost(candidate) or 0) >= 1:
        return "total cost >= 1"
    if _settlement_scope_mismatch(candidate):
        return "event scope mismatch"
    return "total cost >= 1"


def _markets_equivalent(candidate: dict[str, Any]) -> bool:
    if candidate.get("venue_pair") == "polymarket_kalshi":
        return str(candidate.get("confidence_band") or "") in {"high", "medium", ""} and not candidate.get("residual_risk")
    if candidate.get("market_type") in {"game_winner", "championship_winner"} and candidate.get("sportsbook_team"):
        return bool(candidate.get("polymarket_title") or candidate.get("kalshi_title"))
    return False


def _has_no_side(candidate: dict[str, Any]) -> bool:
    return any(_num(candidate.get(key)) is not None for key in ("polymarket_implied_no_price", "polymarket_no_price", "kalshi_implied_no_price", "kalshi_no_price"))


def _has_hedge_leg(candidate: dict[str, Any]) -> bool:
    return any(_num(candidate.get(key)) is not None for key in ("polymarket_implied_yes_price", "polymarket_yes_price", "kalshi_implied_yes_price", "kalshi_yes_price", "sportsbook_implied_probability"))


def _candidate_min_total_cost(candidate: dict[str, Any]) -> float | None:
    totals = []
    pm_yes = _num(candidate.get("polymarket_implied_yes_price") or candidate.get("polymarket_yes_price"))
    pm_no = _num(candidate.get("polymarket_implied_no_price") or candidate.get("polymarket_no_price"))
    kalshi_yes = _num(candidate.get("kalshi_implied_yes_price") or candidate.get("kalshi_yes_price"))
    kalshi_no = _num(candidate.get("kalshi_implied_no_price") or candidate.get("kalshi_no_price"))
    sportsbook = _num(candidate.get("sportsbook_implied_probability"))
    if pm_yes is not None and kalshi_no is not None:
        totals.append(pm_yes + kalshi_no)
    if kalshi_yes is not None and pm_no is not None:
        totals.append(kalshi_yes + pm_no)
    if sportsbook is not None and pm_no is not None:
        totals.append(sportsbook + pm_no)
    if sportsbook is not None and kalshi_no is not None:
        totals.append(sportsbook + kalshi_no)
    return min(totals) if totals else None


def _fully_covers_sportsbook_leg(candidate: dict[str, Any]) -> bool:
    return bool(candidate.get("outcomes_fully_covered") or candidate.get("fully_covers") or candidate.get("market_type") == "game_winner")


def _settlement_scope_mismatch(candidate: dict[str, Any]) -> bool:
    text = f"{candidate.get('reason') or ''} {candidate.get('pricing_reason') or ''}".lower()
    return "scope mismatch" in text or "settlement" in text


def _has_related_markets(candidate: dict[str, Any]) -> bool:
    return bool(candidate.get("venue_pair") and (candidate.get("polymarket_title") or candidate.get("kalshi_title")) and (candidate.get("sportsbook") or candidate.get("sportsbook_team") or candidate.get("kalshi_ticker")))


def _residual_risk(candidate: dict[str, Any], reason: str) -> str:
    if reason == "missing NO side":
        return "Markets are related, but no executable NO/field hedge leg was available to cover all outcomes."
    if reason == "event scope mismatch":
        return "Markets are related, but settlement scope or event definitions may differ across venues."
    if reason == "settlement rules differ":
        return "Markets are related, but venue settlement rules may differ."
    return "Markets are related but not proven equivalent, so residual basis and settlement risk remains."


def _num(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
