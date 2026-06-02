from __future__ import annotations

from collections import Counter
from typing import Any

from src.analysis.cross_market import compare_wallet_markets_to_kalshi
from src.analysis.sports_parser import parse_market_record
from src.analysis.sportsbook_matching import sportsbook_match_diagnostics
from src.analysis.structured_matching import score_structured_sports_pair


def explain_live_matches(polymarket_markets: list[dict[str, Any]], kalshi_markets: list[dict[str, Any]], sportsbook_lines: list[dict[str, Any]] | None = None, accepted_only: bool = False, rejected_only: bool = False, sample_limit: int = 20) -> dict[str, Any]:
    sportsbook_lines = sportsbook_lines or []
    diagnostics: list[dict[str, Any]] = []
    diagnostics.extend(_polymarket_kalshi_diagnostics(polymarket_markets, kalshi_markets))
    diagnostics.extend(sportsbook_match_diagnostics(polymarket_markets, sportsbook_lines, source_venue="polymarket"))
    diagnostics.extend(sportsbook_match_diagnostics(_kalshi_as_polymarket(kalshi_markets), sportsbook_lines, source_venue="kalshi"))
    if accepted_only:
        diagnostics = [item for item in diagnostics if item.get("accepted")]
    if rejected_only:
        diagnostics = [item for item in diagnostics if not item.get("accepted")]
    rejected = [item for item in diagnostics if not item.get("accepted")]
    accepted = [item for item in diagnostics if item.get("accepted")]
    counts = Counter(item.get("rejection_reason") or "accepted" for item in rejected)
    futures = [item for item in diagnostics if item.get("target_market_type") in {"championship_winner", "conference_winner", "division_winner", "season_award"} or item.get("source_market_type") == "championship_winner"]
    futures_rejected = [item for item in futures if not item.get("accepted")]
    futures_accepted = [item for item in futures if item.get("accepted")]
    return {
        "matches_attempted": len(diagnostics),
        "matches_accepted": len(accepted),
        "matches_rejected": len(rejected),
        "top_rejection_reasons": dict(counts.most_common(20)),
        "futures_matches_attempted": len(futures),
        "futures_matches_accepted": len(futures_accepted),
        "futures_matches_rejected": len(futures_rejected),
        "futures_rejection_reasons": dict(Counter(item.get("rejection_reason") or "accepted" for item in futures_rejected).most_common(20)),
        "accepted_matches_sample": accepted[:sample_limit],
        "rejected_matches_sample": rejected[:sample_limit],
        "diagnostics": diagnostics,
    }


def live_match_summary(polymarket_markets: list[dict[str, Any]], kalshi_markets: list[dict[str, Any]], sportsbook_lines: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    diagnostics = explain_live_matches(polymarket_markets, kalshi_markets, sportsbook_lines or [], sample_limit=0)
    return {
        "matches_attempted": diagnostics["matches_attempted"],
        "matches_accepted": diagnostics["matches_accepted"],
        "matches_rejected": diagnostics["matches_rejected"],
        "top_rejection_reasons": diagnostics["top_rejection_reasons"],
        "futures_matches_attempted": diagnostics.get("futures_matches_attempted", 0),
        "futures_matches_accepted": diagnostics.get("futures_matches_accepted", 0),
        "futures_matches_rejected": diagnostics.get("futures_matches_rejected", 0),
        "futures_rejection_reasons": diagnostics.get("futures_rejection_reasons", {}),
    }


def _polymarket_kalshi_diagnostics(polymarket_markets: list[dict[str, Any]], kalshi_markets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    accepted_pairs = {(item.get("polymarket_id"), item.get("kalshi_ticker")) for item in compare_wallet_markets_to_kalshi(polymarket_markets, kalshi_markets, max_candidates=100)}
    rows: list[dict[str, Any]] = []
    parsed_pm = [(market, parse_market_record(market, "polymarket")) for market in polymarket_markets]
    parsed_kalshi = [(market, parse_market_record(market, "kalshi")) for market in kalshi_markets]
    for pm_market, pm in parsed_pm:
        for kalshi_market, kalshi in parsed_kalshi:
            pm_id = str(pm_market.get("conditionId") or pm_market.get("condition_id") or pm_market.get("slug") or pm_market.get("title") or "")
            kalshi_id = str(kalshi_market.get("ticker") or kalshi_market.get("market_ticker") or kalshi_market.get("title") or "")
            accepted = (pm_id, kalshi_id) in accepted_pairs
            candidate = score_structured_sports_pair(pm_market, pm, kalshi_market, kalshi)
            reason = None if accepted else _sports_rejection_reason(pm, kalshi, candidate)
            rows.append({
                "source_venue": "polymarket",
                "target_venue": "kalshi",
                "source_market_title": str(pm_market.get("title") or pm_market.get("question") or pm_market.get("slug") or ""),
                "target_market_title": str(kalshi_market.get("title") or kalshi_market.get("subtitle") or kalshi_market.get("ticker") or ""),
                "source_market_type": pm.market_type,
                "target_market_type": kalshi.market_type,
                "parsed_teams": {"source_team": pm.team, "target_team": kalshi.team},
                "parsed_opponents": {"source_opponent": pm.opponent, "target_opponent": kalshi.opponent},
                "parsed_league": {"source_league": pm.league, "target_league": kalshi.league},
                "parsed_event_date": {"source_season_year": pm.season_year, "target_season_year": kalshi.season_year},
                "confidence_score": candidate.get("similarity_score") if candidate else 0.0,
                "accepted": accepted,
                "rejection_reason": reason,
                "parsed_fields": {"polymarket": pm.to_dict(), "kalshi": kalshi.to_dict()},
            })
    return rows


def _sports_rejection_reason(pm: Any, kalshi: Any, candidate: dict[str, Any] | None) -> str:
    if not pm.league or not pm.team or not kalshi.league:
        return "missing structured fields"
    if pm.league != kalshi.league:
        return "league mismatch"
    if pm.team and kalshi.team and pm.team != kalshi.team and pm.team != kalshi.opponent:
        return "team mismatch"
    if pm.opponent and kalshi.opponent and pm.opponent != kalshi.opponent and pm.opponent != kalshi.team:
        return "opponent mismatch"
    if pm.market_type and kalshi.market_type and pm.market_type != kalshi.market_type:
        return "market type mismatch"
    if pm.season_year and kalshi.season_year and pm.season_year != kalshi.season_year:
        return "event date mismatch"
    if candidate is None:
        return "confidence below threshold"
    return "confidence below threshold"


def _kalshi_as_polymarket(kalshi_markets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for market in kalshi_markets:
        title = str(market.get("title") or market.get("subtitle") or market.get("ticker") or "")
        rows.append({"conditionId": str(market.get("ticker") or title), "title": title, "question": title, "slug": market.get("ticker")})
    return rows
