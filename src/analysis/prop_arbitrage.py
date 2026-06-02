from __future__ import annotations

from collections import Counter
from typing import Any

from src.analysis.prop_matching import match_prop_pairs


def scan_prop_arbitrage(props: list[dict[str, Any]], bankroll: float | None = None, min_guaranteed_roi: float | None = None) -> dict[str, Any]:
    pairs, rejects = match_prop_pairs(props)
    arbs: list[dict[str, Any]] = []
    pair_rejects: list[dict[str, Any]] = []
    for pair in pairs:
        over = pair["over"]
        under = pair["under"]
        total = round(float(over.get("implied_probability") or 999) + float(under.get("implied_probability") or 999), 6)
        if total >= 1:
            pair_rejects.append({
                "player": pair.get("player"),
                "market_type": pair.get("market_type"),
                "line": pair.get("line"),
                "rejection_reason": "total implied probability >= 1",
                "implied_probability_sum": total,
            })
            continue
        arb = {
            "opportunity_type": "true_arbitrage",
            "player": pair.get("player"),
            "prop_type": pair.get("market_type"),
            "line": pair.get("line"),
            "over_book": over.get("bookmaker"),
            "over_odds": over.get("odds"),
            "under_book": under.get("bookmaker"),
            "under_odds": under.get("odds"),
            "implied_probability_sum": total,
            "guaranteed_profit": round(1 - total, 6),
            "guaranteed_roi": round((1 - total) / total, 6),
            "over": over,
            "under": under,
        }
        if bankroll is not None:
            arb.update(_stake_size(float(over.get("implied_probability") or 0), float(under.get("implied_probability") or 0), bankroll, total))
        arbs.append(arb)
    if min_guaranteed_roi is not None:
        arbs = [arb for arb in arbs if (arb.get("guaranteed_roi") or 0) >= min_guaranteed_roi]
    reasons = Counter(item["rejection_reason"] for item in rejects + pair_rejects)
    return {
        "props_fetched": len(props),
        "normalized_props": len(props),
        "matched_prop_pairs": len(pairs),
        "rejected_pairs": len(rejects) + len(pair_rejects),
        "rejection_reasons": dict(reasons),
        "prop_arbitrage_candidates": sorted(arbs, key=lambda item: item.get("guaranteed_roi") or 0, reverse=True),
        "diagnostics": [_compact_match_reject(item) for item in rejects] + pair_rejects,
    }


def _stake_size(over_prob: float, under_prob: float, bankroll: float, total: float) -> dict[str, Any]:
    if total <= 0:
        return {"stake_over": 0.0, "stake_under": 0.0, "guaranteed_profit_amount": 0.0}
    return {
        "stake_over": round(float(bankroll) * over_prob / total, 2),
        "stake_under": round(float(bankroll) * under_prob / total, 2),
        "guaranteed_profit_amount": round(float(bankroll) * ((1 - total) / total), 2),
    }


def _compact_match_reject(item: dict[str, Any]) -> dict[str, Any]:
    left = item.get("left") or {}
    right = item.get("right") or {}
    return {
        "player": left.get("player") or right.get("player"),
        "market_type": left.get("market_type") or right.get("market_type"),
        "line": left.get("line") if left.get("line") is not None else right.get("line"),
        "rejection_reason": item.get("rejection_reason"),
        "left_player": left.get("player"),
        "right_player": right.get("player"),
        "left_market_type": left.get("market_type"),
        "right_market_type": right.get("market_type"),
        "left_line": left.get("line"),
        "right_line": right.get("line"),
        "left_side": left.get("side"),
        "right_side": right.get("side"),
    }
