from __future__ import annotations

from collections import Counter
from typing import Any

from src.analysis.cross_market import _dedupe_candidates, _unique_polymarket_markets, score_market_pair
from src.analysis.crypto_matching import structured_crypto_candidates_with_diagnostics
from src.analysis.market_normalization import normalize_kalshi_market, normalize_polymarket_market
from src.analysis.structured_matching import structured_sports_candidates


def explain_market_matches(trades: list[dict[str, Any]], kalshi_markets: list[dict[str, Any]], max_candidates: int = 10) -> dict[str, Any]:
    polymarket_markets = _unique_polymarket_markets(trades)
    sports = structured_sports_candidates(polymarket_markets, kalshi_markets, max_candidates=max_candidates * 2)
    crypto, crypto_diagnostics = structured_crypto_candidates_with_diagnostics(polymarket_markets, kalshi_markets, max_candidates=max_candidates * 2)
    fallback = _fallback_text_candidates(polymarket_markets, kalshi_markets, max_candidates=max_candidates * 2)
    accepted = _dedupe_candidates(sports + crypto + fallback, max_candidates)
    rejected = [item for item in crypto_diagnostics if not item.get("accepted")]
    rejected_counts = Counter(item.get("reason", "unknown") for item in rejected)
    return {
        "polymarket_markets_inspected": len(polymarket_markets),
        "kalshi_markets_inspected": len(kalshi_markets),
        "sports_structured_matches": len(sports),
        "crypto_structured_matches": len(crypto),
        "fallback_text_matches": len(fallback),
        "top_rejected_candidate_reasons": [
            {"reason": reason, "count": count} for reason, count in rejected_counts.most_common(10)
        ],
        "accepted_matches": accepted,
        "diagnostics": [item for item in crypto_diagnostics if item.get("accepted")] + rejected[:50],
    }


def _fallback_text_candidates(polymarket_markets: list[dict[str, Any]], kalshi_markets: list[dict[str, Any]], max_candidates: int) -> list[dict[str, Any]]:
    normalized_pm = [normalize_polymarket_market(market) for market in polymarket_markets]
    normalized_kalshi = [normalize_kalshi_market(market) for market in kalshi_markets]
    candidates: list[dict[str, Any]] = []
    for pm_market in normalized_pm:
        for kalshi_market in normalized_kalshi:
            candidate = score_market_pair(pm_market, kalshi_market)
            if candidate:
                candidates.append(candidate)
    candidates.sort(key=lambda item: (item["similarity_score"], len(item["shared_keywords_entities"])), reverse=True)
    return _dedupe_candidates(candidates, max_candidates)
