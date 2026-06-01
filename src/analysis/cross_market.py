from __future__ import annotations

from collections import defaultdict
from typing import Any

from src.analysis.crypto_matching import structured_crypto_candidates
from src.analysis.market_normalization import NormalizedMarketText, normalize_kalshi_market, normalize_polymarket_market, text_similarity
from src.analysis.structured_matching import structured_sports_candidates
from src.analysis.volume import money_value


def compare_wallet_markets_to_kalshi(trades: list[dict[str, Any]], kalshi_markets: list[dict[str, Any]], max_candidates: int = 10) -> list[dict[str, Any]]:
    polymarket_markets = _unique_polymarket_markets(trades)
    candidates: list[dict[str, Any]] = structured_sports_candidates(polymarket_markets, kalshi_markets, max_candidates=max_candidates * 2)
    candidates.extend(structured_crypto_candidates(polymarket_markets, kalshi_markets, max_candidates=max_candidates * 2))

    normalized_pm = [normalize_polymarket_market(market) for market in polymarket_markets]
    normalized_kalshi = [normalize_kalshi_market(market) for market in kalshi_markets]
    for pm_market in normalized_pm:
        for kalshi_market in normalized_kalshi:
            candidate = score_market_pair(pm_market, kalshi_market)
            if candidate:
                candidates.append(candidate)

    candidates.sort(key=lambda item: (item["similarity_score"], len(item["shared_keywords_entities"])), reverse=True)
    return _dedupe_candidates(candidates, max_candidates)


def _dedupe_candidates(candidates: list[dict[str, Any]], max_candidates: int) -> list[dict[str, Any]]:
    deduped: list[dict[str, Any]] = []
    seen_pairs: set[tuple[str, str]] = set()
    for candidate in candidates:
        pair = (candidate["polymarket_id"], candidate["kalshi_ticker"])
        if pair in seen_pairs:
            continue
        seen_pairs.add(pair)
        deduped.append(candidate)
        if len(deduped) >= max_candidates:
            break
    return deduped


def score_market_pair(pm_market: NormalizedMarketText, kalshi_market: NormalizedMarketText) -> dict[str, Any] | None:
    shared_tokens = (pm_market.tokens & kalshi_market.tokens) - {"market"}
    shared_entities = pm_market.entities & kalshi_market.entities
    category_match = pm_market.category_guess == kalshi_market.category_guess and pm_market.category_guess != "Other"
    league_match = bool(pm_market.league_guess and pm_market.league_guess == kalshi_market.league_guess)
    title_ratio = text_similarity(pm_market.normalized_title, kalshi_market.normalized_title)

    if pm_market.category_guess != "Other" and kalshi_market.category_guess != "Other" and pm_market.category_guess != kalshi_market.category_guess:
        return None
    if pm_market.league_guess and kalshi_market.league_guess and pm_market.league_guess != kalshi_market.league_guess:
        return None
    if len(shared_tokens) < 2 and not shared_entities:
        return None

    token_union = pm_market.tokens | kalshi_market.tokens
    token_score = len(shared_tokens) / len(token_union) if token_union else 0.0
    entity_bonus = min(0.18, 0.06 * len(shared_entities))
    category_bonus = 0.08 if category_match else 0.0
    league_bonus = 0.08 if league_match else 0.0
    score = round((0.48 * title_ratio) + (0.44 * token_score) + entity_bonus + category_bonus + league_bonus, 4)

    if score < 0.42:
        return None
    if score < 0.55 and len(shared_tokens) < 3 and not shared_entities:
        return None

    confidence = "high" if score >= 0.72 and (len(shared_tokens) >= 4 or shared_entities) else "medium" if score >= 0.56 else "low"
    shared = sorted(shared_tokens | shared_entities)
    reason = _reason(shared, pm_market, kalshi_market, title_ratio, category_match, league_match)
    return {
        "normalized_title": pm_market.normalized_title,
        "polymarket_id": pm_market.source_id,
        "polymarket_title": pm_market.title,
        "kalshi_ticker": kalshi_market.source_id,
        "kalshi_title": kalshi_market.title,
        "shared_keywords_entities": shared,
        "sport_league_category_guess": {
            "category": pm_market.category_guess if pm_market.category_guess != "Other" else kalshi_market.category_guess,
            "league": pm_market.league_guess or kalshi_market.league_guess,
        },
        "similarity_score": score,
        "confidence_band": confidence,
        "reason": reason,
    }


def _unique_polymarket_markets(trades: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    volumes: dict[str, float] = defaultdict(float)
    for trade in trades:
        key = str(trade.get("conditionId") or trade.get("slug") or trade.get("title") or "unknown")
        volumes[key] += money_value(trade)
        grouped.setdefault(key, trade)
    return [grouped[key] for key, _volume in sorted(volumes.items(), key=lambda item: item[1], reverse=True)]


def _reason(shared: list[str], pm_market: NormalizedMarketText, kalshi_market: NormalizedMarketText, title_ratio: float, category_match: bool, league_match: bool) -> str:
    parts = []
    if shared:
        parts.append("shared terms: " + ", ".join(shared[:6]))
    if category_match:
        parts.append(f"both look like {pm_market.category_guess} markets")
    if league_match:
        parts.append(f"league guess matches: {pm_market.league_guess}")
    if title_ratio >= 0.5:
        parts.append("market titles are textually similar")
    return "; ".join(parts) or "conservative text overlap match"
