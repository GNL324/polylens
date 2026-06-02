from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path
from typing import Any

from src.adapters.kalshi import KalshiClient
from src.adapters.odds_api import MissingOddsAPIKey, OddsAPIClient
from src.adapters.polymarket import PolymarketClient
from src.alerts.notifier import MissingWebhookURLError, build_notifier
from src.analysis.arb_pricing import enrich_candidates_with_pricing, enrich_sportsbook_candidates_with_pricing
from src.analysis.arb_signals import detect_signals
from src.analysis.cross_market import compare_wallet_markets_to_kalshi
from src.analysis.futures_inventory import summarize_futures_inventory
from src.analysis.hedge_leg_discovery import explain_hedge_search
from src.analysis.hedged_arbitrage import classify_arbitrage_candidates
from src.analysis.kalshi_inventory_filter import filter_kalshi_inventory
from src.analysis.live_arbitrage import scan_live_arbitrage
from src.analysis.live_match_diagnostics import explain_live_matches as explain_live_market_matches
from src.analysis.market_inventory import summarize_market_inventory
from src.analysis.multibook_arbitrage import scan_multibook_arbitrage
from src.analysis.markets import summarize_markets
from src.analysis.match_diagnostics import explain_market_matches
from src.analysis.odds_normalization import normalize_futures_events, normalize_odds_events
from src.analysis.pnl import summarize_pnl
from src.analysis.sportsbook_matching import match_sportsbook_lines
from src.analysis.synthetic_field import debug_synthetic_field as build_debug_synthetic_field
from src.analysis.timing import summarize_timing
from src.analysis.volume import summarize_volume
from src.analysis.watch_mode import watch_live_arbitrage
from src.reports.wallet_report import WalletReport
from src.storage.opportunity_store import OpportunityStore


def setup_logging() -> None:
    Path("logs").mkdir(exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=[logging.FileHandler("logs/polylens.log"), logging.StreamHandler()],
    )


def build_wallet_report(wallet: str, include_kalshi: bool = False, include_pricing: bool = False) -> WalletReport:
    logger = logging.getLogger(__name__)
    client = PolymarketClient(raw_dir="data/raw")
    logger.info("starting wallet analysis wallet=%s include_kalshi=%s include_pricing=%s", wallet, include_kalshi, include_pricing)
    profile = client.get_public_profile(wallet)
    trades = client.get_user_trades(wallet)
    activity = client.get_user_activity(wallet)
    positions = client.get_positions(wallet)
    logger.info("parsing payloads trades=%s activity=%s positions=%s", len(trades), len(activity), len(positions))

    volume = summarize_volume(trades)
    markets = summarize_markets(trades, positions)
    timing = summarize_timing(trades)
    pnl = summarize_pnl(positions, trades)
    behavior = detect_signals(trades, positions, markets, volume)
    cross_platform_candidates: list[dict[str, Any]] = []
    price_aware_candidates: list[dict[str, Any]] = []
    if include_kalshi or include_pricing:
        cross_platform_candidates, kalshi_markets = compare_kalshi_candidates(trades, return_markets=True)
        if include_pricing:
            price_aware_candidates = enrich_candidates_with_pricing(cross_platform_candidates, trades, kalshi_markets)

    limitations = [
        "PnL is estimated from currently available position fields and may omit resolved markets no longer returned by the positions endpoint.",
        "Kalshi arbitrage detection is heuristic and compares public market text only; it does not prove executable arbitrage.",
        "Price-aware arbitrage uses Kalshi public prices and Polymarket wallet trade prices; it is not a live executable quote check.",
        "Categories are inferred from market text until Gamma tag enrichment is expanded.",
    ]

    username = profile.get("name") or profile.get("pseudonym") or (trades[0].get("name") if trades else None) or (trades[0].get("pseudonym") if trades else None)
    report = WalletReport(
        wallet_address=wallet,
        username=username,
        trade_count=volume["trade_count"],
        volume=volume["total_volume"],
        average_trade_size=volume["average_trade_size"],
        largest_trade=volume["largest_trade"],
        largest_trade_size=volume["largest_trade_size"],
        favorite_markets=markets["favorite_markets"],
        favorite_categories=markets["favorite_categories"],
        market_concentration=markets["market_concentration"],
        trading_schedule=timing,
        estimated_pnl=pnl["estimated_pnl"],
        exposure=pnl["exposure"],
        position_sizing={
            "position_count": pnl["position_count"],
            "average_position_size": pnl["average_position_size"],
            "largest_position_size": pnl["largest_position_size"],
        },
        behavior_classification=behavior["behavior_classification"],
        confidence_score=behavior["confidence_score"],
        signals=behavior["signals"],
        raw_counts={"trades": len(trades), "activity": len(activity), "positions": len(positions)},
        cross_platform_arbitrage_candidates=cross_platform_candidates,
        price_aware_arbitrage_candidates=price_aware_candidates,
        limitations=limitations,
    )
    logger.info(
        "analysis complete wallet=%s classification=%s kalshi_candidates=%s price_candidates=%s",
        wallet,
        report.behavior_classification,
        len(cross_platform_candidates),
        len(price_aware_candidates),
    )
    return report


def compare_kalshi_candidates(trades: list[dict[str, Any]], return_markets: bool = False):
    logger = logging.getLogger(__name__)
    kalshi_client = KalshiClient(raw_dir="data/raw")
    try:
        kalshi_markets = kalshi_client.get_markets(status="open")
    except Exception as exc:
        logger.warning("Kalshi market ingestion failed; returning no candidates: %s", exc)
        return ([], []) if return_markets else []
    candidates = compare_wallet_markets_to_kalshi(trades, kalshi_markets)
    logger.info("cross-market comparison candidates=%s kalshi_markets=%s", len(candidates), len(kalshi_markets))
    return (candidates, kalshi_markets) if return_markets else candidates


def analyze_wallet(wallet: str) -> WalletReport:
    report = build_wallet_report(wallet)
    output = report.save("data/reports")
    logging.getLogger(__name__).info("saved report %s", output)
    print(report.summary_text())
    print(f"\nSaved JSON report: {output}")
    return report


def export_wallet(wallet: str, include_kalshi: bool = False, include_pricing: bool = False) -> WalletReport:
    report = build_wallet_report(wallet, include_kalshi=include_kalshi, include_pricing=include_pricing)
    output = report.save("data/reports")
    logging.getLogger(__name__).info("exported report %s", output)
    print(output)
    return report


def compare_kalshi(wallet: str) -> WalletReport:
    report = build_wallet_report(wallet, include_kalshi=True)
    output = report.save("data/reports")
    print(f"Possible Polymarket/Kalshi overlap candidates: {len(report.cross_platform_arbitrage_candidates)}")
    for candidate in report.cross_platform_arbitrage_candidates[:10]:
        print(
            f"- {candidate['confidence_band']} {candidate['similarity_score']:.2f}: "
            f"{candidate['polymarket_title']} <> {candidate['kalshi_title']} ({candidate['kalshi_ticker']})"
        )
        print(f"  {candidate['reason']}")
    print(f"\nSaved JSON report: {output}")
    return report


def scan_arb(wallet: str) -> WalletReport:
    report = build_wallet_report(wallet, include_kalshi=True, include_pricing=True)
    output = report.save("data/reports")
    candidates = report.price_aware_arbitrage_candidates
    print(f"Price-aware arbitrage candidates: {len(candidates)}")
    for candidate in candidates[:10]:
        status = candidate.get("pricing_status")
        edge = candidate.get("estimated_edge")
        edge_text = "insufficient pricing data" if edge is None else f"edge={edge:.4f}"
        print(
            f"- {candidate.get('confidence_band')} {candidate.get('similarity_score', 0):.2f}: "
            f"{candidate.get('polymarket_title')} <> {candidate.get('kalshi_title')} ({status}, {edge_text})"
        )
        print(f"  {candidate.get('pricing_reason') or candidate.get('reason')}")
    print(f"\nSaved JSON report: {output}")
    return report


def explain_matches(wallet: str, as_json: bool = False, save: bool = False, db_path: str = "data/polylens.db") -> dict[str, Any]:
    logger = logging.getLogger(__name__)
    poly_client = PolymarketClient(raw_dir="data/raw")
    kalshi_client = KalshiClient(raw_dir="data/raw")
    polymarket_markets = poly_client.get_wallet_markets(wallet, include_market_details=True)
    try:
        kalshi_markets = kalshi_client.get_markets(status="open")
    except Exception as exc:
        logger.warning("Kalshi market ingestion failed during diagnostics: %s", exc)
        kalshi_markets = []
    diagnostics = explain_market_matches(polymarket_markets, kalshi_markets)
    if save:
        store = OpportunityStore(db_path)
        scan_run_id = store.save_scan_run(scan_mode="explain-matches", filters={"wallet": wallet}, venues_scanned={"polymarket": diagnostics.get("polymarket_markets_inspected"), "kalshi": diagnostics.get("kalshi_markets_inspected")}, candidate_count=len(diagnostics.get("accepted_matches", [])), skipped_summary={"top_rejected_candidate_reasons": diagnostics.get("top_rejected_candidate_reasons", [])}, raw_result=diagnostics)
        for item in diagnostics.get("diagnostics", []):
            if not item.get("accepted"):
                store.save_rejected_candidate(item, scan_run_id=scan_run_id)
        diagnostics["saved_scan_run_id"] = scan_run_id
    if as_json:
        print(json.dumps(diagnostics, indent=2, sort_keys=True))
        return diagnostics
    print("Polylens Match Diagnostics")
    print("=" * 27)
    print(f"Polymarket markets inspected: {diagnostics['polymarket_markets_inspected']}")
    print(f"Kalshi markets inspected: {diagnostics['kalshi_markets_inspected']}")
    print(f"Sports structured matches: {diagnostics['sports_structured_matches']}")
    print(f"Crypto structured matches: {diagnostics['crypto_structured_matches']}")
    print(f"Fallback text matches: {diagnostics['fallback_text_matches']}")
    print("Top rejected candidate reasons:")
    for item in diagnostics["top_rejected_candidate_reasons"][:10]:
        print(f"- {item['count']}: {item['reason']}")
    if not diagnostics["top_rejected_candidate_reasons"]:
        print("- none recorded")
    print(f"Likely zero-candidate reason: {diagnostics.get('likely_zero_candidate_reason')}")
    print(f"Polymarket open/closed: {diagnostics.get('polymarket_open_count')}/{diagnostics.get('polymarket_closed_count')}")
    print(f"Kalshi open/closed: {diagnostics.get('kalshi_open_count')}/{diagnostics.get('kalshi_closed_count')}")
    print(f"Unparsed Polymarket/Kalshi: {diagnostics.get('unparsed_polymarket_count')}/{diagnostics.get('unparsed_kalshi_count')}")
    print(f"Accepted matches: {len(diagnostics['accepted_matches'])}")
    for candidate in diagnostics["accepted_matches"][:10]:
        print(f"- {candidate.get('confidence_band')} {candidate.get('similarity_score', 0):.2f}: {candidate.get('polymarket_title')} <> {candidate.get('kalshi_title')}")
        print(f"  {candidate.get('reason')}")
    return diagnostics


def market_inventory(wallet: str, include_closed: bool = False, as_json: bool = False) -> dict[str, Any]:
    logger = logging.getLogger(__name__)
    poly_client = PolymarketClient(raw_dir="data/raw")
    kalshi_client = KalshiClient(raw_dir="data/raw")
    polymarket_markets = poly_client.get_wallet_markets(wallet, include_market_details=True)
    try:
        kalshi_markets = kalshi_client.get_markets(status="open", include_closed=include_closed)
    except Exception as exc:
        logger.warning("Kalshi market ingestion failed during inventory: %s", exc)
        kalshi_markets = []
    inventory = summarize_market_inventory(polymarket_markets, kalshi_markets)
    if as_json:
        print(json.dumps(inventory, indent=2, sort_keys=True))
        return inventory
    print("Polylens Market Inventory")
    print("=" * 26)
    print(f"Polymarket markets: {inventory['polymarket']['total_count']}")
    print(f"Kalshi markets: {inventory['kalshi']['total_count']}")
    print(f"Polymarket open/closed/unknown: {inventory['polymarket_open_count']}/{inventory['polymarket_closed_count']}/{inventory['polymarket']['status_counts'].get('unknown', 0)}")
    print(f"Kalshi open/closed/unknown: {inventory['kalshi_open_count']}/{inventory['kalshi_closed_count']}/{inventory['kalshi']['status_counts'].get('unknown', 0)}")
    print(f"Polymarket categories: {inventory['polymarket']['category_counts']}")
    print(f"Kalshi categories: {inventory['kalshi']['category_counts']}")
    print(f"Crypto market types: {inventory['polymarket']['crypto_market_types_found']}")
    print(f"Sports market types: {inventory['polymarket']['sports_market_types_found']}")
    print(f"Unparsed Polymarket markets: {inventory['unparsed_polymarket_count']}")
    print(f"Unparsed Kalshi markets: {inventory['unparsed_kalshi_count']}")
    print("Top reasons no matches were available:")
    for reason in inventory["top_reasons_no_matches_available"]:
        print(f"- {reason}")
    return inventory


def list_sportsbooks(as_json: bool = False) -> list[dict[str, Any]]:
    client = OddsAPIClient(raw_dir="data/raw")
    sports = client.list_sports()
    if as_json:
        print(json.dumps(sports, indent=2, sort_keys=True))
    else:
        for sport in sports:
            print(f"{sport.get('key')} - {sport.get('title')} ({sport.get('group')})")
    return sports


def fetch_odds(sport_key: str, bookmaker: str | None = None, region: str = "us", markets: str = "h2h,spreads,totals", as_json: bool = False, quiet: bool = False) -> list[dict[str, Any]]:
    client = OddsAPIClient(raw_dir="data/raw")
    events = client.get_odds(sport_key, regions=region, markets=markets, bookmakers=bookmaker)
    normalized = normalize_odds_events(events)
    if as_json:
        print(json.dumps(normalized, indent=2, sort_keys=True))
    elif not quiet:
        print(f"Fetched sportsbook lines: {len(normalized)}")
        for line in normalized[:20]:
            print(f"- {line.get('bookmaker_name')} {line.get('league')} {line.get('market_type')}: {line.get('team')} {line.get('odds')} p={line.get('implied_probability')}")
    return normalized


def fetch_futures(sport_key: str, bookmaker: str | None = None, region: str = "us", as_json: bool = False, quiet: bool = False) -> dict[str, Any]:
    client = OddsAPIClient(raw_dir="data/raw")
    payload = client.get_futures(sport_key, regions=region, bookmakers=bookmaker)
    if not payload.get("supported"):
        result = {"supported": False, "reason": payload.get("reason") or "no futures endpoint for sport", "sport_key": sport_key, "futures_sport_key": payload.get("futures_sport_key"), "futures": []}
        if as_json:
            print(json.dumps(result, indent=2, sort_keys=True))
        elif not quiet:
            print(result["reason"])
        return result
    raw_events = payload.get("events") or []
    normalized = normalize_futures_events(raw_events)
    inventory = summarize_futures_inventory(raw_events, normalized, normalized)
    result = {"supported": True, "reason": None, "sport_key": sport_key, "futures_sport_key": payload.get("futures_sport_key"), "futures": normalized, "inventory": inventory, "futures_raw_outcomes": inventory["futures_raw_outcomes"], "futures_normalized_outcomes": inventory["futures_normalized_outcomes"], "futures_retained_outcomes": inventory["futures_retained_outcomes"]}
    if as_json:
        print(json.dumps(result, indent=2, sort_keys=True))
    elif not quiet:
        print(f"Fetched sportsbook futures: {len(normalized)}")
        for row in normalized[:20]:
            print(f"- {row.get('league')} {row.get('market_type')} {row.get('team')} {row.get('bookmaker_name')} odds={row.get('odds')} implied={row.get('implied_probability')}")
    return result


def scan_sportsbook_arb(wallet: str, sport_key: str, bookmaker: str | None = None, region: str = "us", as_json: bool = False) -> list[dict[str, Any]]:
    poly_client = PolymarketClient(raw_dir="data/raw")
    trades = poly_client.get_user_trades(wallet)
    odds_lines = fetch_odds(sport_key, bookmaker=bookmaker, region=region, markets="h2h,spreads,totals,outrights", quiet=True)
    candidates = match_sportsbook_lines(trades, odds_lines)
    priced = enrich_sportsbook_candidates_with_pricing(candidates, trades)
    if as_json:
        print(json.dumps(priced, indent=2, sort_keys=True))
    else:
        print(f"Sportsbook arbitrage candidates: {len(priced)}")
        for candidate in priced[:20]:
            edge = candidate.get("estimated_edge")
            edge_text = "insufficient pricing data" if edge is None else f"edge={edge:.4f}"
            print(f"- {candidate.get('confidence_band')} {candidate.get('polymarket_title')} <> {candidate.get('sportsbook')} {candidate.get('sportsbook_team')} ({edge_text})")
            print(f"  {candidate.get('pricing_reason') or candidate.get('reason')}")
    return priced


def debug_futures_inventory(sport_key: str, bookmaker: str | None = None, as_json: bool = False) -> dict[str, Any]:
    client = OddsAPIClient(raw_dir="data/raw")
    payload = client.get_futures(sport_key, bookmakers=bookmaker)
    raw_events = payload.get("events") or [] if payload.get("supported") else []
    normalized = normalize_futures_events(raw_events)
    summary = summarize_futures_inventory(raw_events, normalized, normalized)
    summary.update({
        "supported": payload.get("supported"),
        "reason": payload.get("reason"),
        "requested_sport_key": sport_key,
        "futures_sport_key": payload.get("futures_sport_key"),
        "bookmaker": bookmaker,
    })
    if as_json:
        print(json.dumps(summary, indent=2, sort_keys=True))
    else:
        print("Polylens Futures Inventory Debug")
        print("=" * 32)
        print(f"Requested sport: {sport_key}")
        print(f"Futures sport: {summary.get('futures_sport_key')}")
        print(f"Raw outcomes: {summary['futures_raw_outcomes']}")
        print(f"Normalized outcomes: {summary['futures_normalized_outcomes']}")
        print(f"Retained outcomes: {summary['futures_retained_outcomes']}")
        for market in summary["markets"]:
            print(f"- {market.get('bookmaker')} {market.get('market_key')} outcomes={market.get('outcome_count')}: {', '.join(str(name) for name in market.get('outcome_names', []))}")
    return summary


def debug_synthetic_field_cli(sport_key: str | None = None, selected_team: str | None = None, as_json: bool = False) -> dict[str, Any]:
    if not sport_key or not selected_team:
        raise ValueError("--sport and --team are required")
    futures = fetch_futures(sport_key, quiet=True)
    outcomes = futures.get("futures") if futures.get("supported") else []
    result = build_debug_synthetic_field(outcomes or [], selected_team)
    if as_json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print("Polylens Synthetic Field Debug")
        print("=" * 31)
        print(f"Selected team: {result['selected_team']}")
        print(f"Field members: {result['field_members']}")
        print(f"Best book per outcome: {result['best_book_per_outcome']}")
        print(f"Implied probability sum: {result['implied_probability_sum']}")
    return result


def scan_multibook_arb(sport_key: str | None = None, keyword: str | None = None, limit: int = 100, bankroll: float | None = None, min_guaranteed_roi: float | None = None, as_json: bool = False) -> dict[str, Any]:
    lines: list[dict[str, Any]] = []
    if sport_key:
        try:
            lines.extend(fetch_odds(sport_key, markets="h2h", quiet=True))
        except MissingOddsAPIKey:
            pass
        try:
            futures = fetch_futures(sport_key, quiet=True)
            if futures.get("supported"):
                lines.extend(futures.get("futures") or [])
        except MissingOddsAPIKey:
            pass
    result = scan_multibook_arbitrage(lines, bankroll=bankroll, min_guaranteed_roi=min_guaranteed_roi)
    if as_json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print("Polylens Multibook Arbitrage")
        print("=" * 29)
        print(f"Sportsbook multibook arbs: {len(result['sportsbook_multibook_arbs'])}")
        print(f"Synthetic field arbs: {len(result['synthetic_field_arbs'])}")
        print(f"Incomplete hedges: {len(result['incomplete_hedges'])}")
    return result


def find_hedges(keyword: str | None = None, sport_key: str | None = None, limit: int = 100, as_json: bool = False) -> dict[str, Any]:
    result = scan_live_arb(sport_key=sport_key, keyword=keyword, limit=limit, as_json=False, include_low_confidence=True)
    candidates = result.get("top_scored_candidates") or result.get("top_candidates") or []
    diagnostics = [explain_hedge_search(candidate, candidate.get("hedge_inventory") or []) for candidate in candidates]
    counts: dict[str, int] = {}
    for item in diagnostics:
        key = item.get("diagnostic") or "unknown"
        counts[key] = counts.get(key, 0) + 1
    output = {"hedge_diagnostics": diagnostics, "diagnostic_counts": counts}
    if as_json:
        print(json.dumps(output, indent=2, sort_keys=True))
    else:
        print("Polylens Hedge Discovery")
        print("=" * 25)
        print(f"Candidates inspected: {len(diagnostics)}")
        print(f"Diagnostics: {counts}")
    return output


def scan_true_arb(
    keyword: str | None = None,
    sport_key: str | None = None,
    limit: int = 100,
    bankroll: float | None = None,
    min_guaranteed_roi: float | None = None,
    include_hedges: bool = False,
    as_json: bool = False,
) -> dict[str, Any]:
    result = scan_live_arb(sport_key=sport_key, keyword=keyword, limit=limit, as_json=False, include_low_confidence=include_hedges)
    all_candidates = result.get("top_scored_candidates") or result.get("top_candidates") or []
    classified = classify_arbitrage_candidates(all_candidates, bankroll=bankroll, include_hedges=include_hedges)
    true_arbs = classified["true_arbitrage_candidates"]
    if min_guaranteed_roi is not None:
        true_arbs = [item for item in true_arbs if (item.get("guaranteed_roi") or 0) >= min_guaranteed_roi]
    multibook = scan_multibook_arb(sport_key=sport_key, keyword=keyword, limit=limit, bankroll=bankroll, min_guaranteed_roi=min_guaranteed_roi, as_json=False) if sport_key else {"sportsbook_multibook_arbs": [], "synthetic_field_arbs": [], "incomplete_hedges": []}
    output = {
        "prediction_market_true_arbs": true_arbs,
        "true_arbitrage_candidates": true_arbs,
        "sportsbook_multibook_arbs": multibook["sportsbook_multibook_arbs"],
        "synthetic_field_arbs": multibook["synthetic_field_arbs"],
        "incomplete_hedges": multibook["incomplete_hedges"],
        "cross_market_hedge_candidates": classified["cross_market_hedge_candidates"] if include_hedges else [],
        "positive_ev_candidates": classified["positive_ev_candidates"],
        "diagnostics": {
            "candidates_scanned": len(all_candidates),
            "true_arbitrage_count": len(true_arbs),
            "cross_market_hedge_count": len(classified["cross_market_hedge_candidates"]) if include_hedges else 0,
            "positive_ev_count": len(classified["positive_ev_candidates"]),
        },
    }
    if as_json:
        print(json.dumps(output, indent=2, sort_keys=True))
    else:
        print("Polylens True Arbitrage Scan")
        print("=" * 29)
        print(f"True arbitrage candidates: {len(true_arbs)}")
        print(f"Cross-market hedges: {len(output['cross_market_hedge_candidates'])}")
        print(f"Positive-EV candidates: {len(output['positive_ev_candidates'])}")
    return output


def scan_live_arb(
    sport_key: str | None = None,
    keyword: str | None = None,
    category: str | None = None,
    limit: int = 100,
    region: str = "us",
    bookmaker: str | None = None,
    as_json: bool = False,
    min_edge: float | None = None,
    min_score: float | None = None,
    max_close_hours: float | None = None,
    include_low_confidence: bool = False,
    save: bool = False,
    db_path: str = "data/polylens.db",
) -> dict[str, Any]:
    logger = logging.getLogger(__name__)
    venue_errors: dict[str, str] = {}
    poly_client = PolymarketClient(raw_dir="data/raw")
    kalshi_client = KalshiClient(raw_dir="data/raw")
    try:
        polymarket_markets = poly_client.get_active_markets(keyword=keyword, category=category, sport=sport_key, limit=limit)
    except Exception as exc:
        logger.warning("Polymarket live market discovery failed: %s", exc)
        polymarket_markets = []
        venue_errors["polymarket"] = f"Polymarket live discovery failed: {exc}"
    kalshi_inventory_diagnostics: dict[str, Any] = {}
    try:
        kalshi_markets_raw = kalshi_client.get_markets(status="open", limit=min(max(limit, 1), 1000), max_pages=5)
        kalshi_markets, kalshi_inventory_diagnostics = filter_kalshi_inventory(kalshi_markets_raw)
    except Exception as exc:
        logger.warning("Kalshi live market discovery failed: %s", exc)
        kalshi_markets = []
        venue_errors["kalshi"] = f"Kalshi live discovery failed: {exc}"

    sportsbook_lines: list[dict[str, Any]] = []
    sportsbook_futures: list[dict[str, Any]] = []
    sportsbook_skipped_reason: str | None = None
    if sport_key:
        try:
            sportsbook_lines = fetch_odds(sport_key, bookmaker=bookmaker, region=region, markets="h2h,spreads,totals", quiet=True)
            futures_result = fetch_futures(sport_key, bookmaker=bookmaker, region=region, quiet=True)
            if futures_result.get("supported"):
                sportsbook_futures = futures_result.get("futures") or []
                sportsbook_lines.extend(sportsbook_futures)
        except MissingOddsAPIKey:
            sportsbook_skipped_reason = "ODDS_API_KEY missing; sportsbook side skipped"
        except Exception as exc:
            logger.warning("Sportsbook odds fetch failed: %s", exc)
            sportsbook_skipped_reason = f"sportsbook odds fetch failed: {exc}"
    else:
        sportsbook_skipped_reason = "--sport not provided; sportsbook side skipped"

    result = scan_live_arbitrage(
        polymarket_markets,
        kalshi_markets,
        sportsbook_lines=sportsbook_lines,
        sportsbook_skipped_reason=sportsbook_skipped_reason,
        venue_errors=venue_errors,
        min_edge=min_edge,
        min_score=min_score,
        max_close_hours=max_close_hours,
        include_low_confidence=include_low_confidence,
    )
    pm_diagnostics = getattr(poly_client, "last_active_market_search_diagnostics", {}) or {}
    if not isinstance(pm_diagnostics, dict):
        pm_diagnostics = {}
    result["polymarket_raw_markets"] = pm_diagnostics.get("raw_markets_returned", len(polymarket_markets))
    result["polymarket_filtered_markets"] = pm_diagnostics.get("filtered_markets_retained", len(polymarket_markets))
    result["polymarket_discarded_markets"] = pm_diagnostics.get("markets_discarded", 0)
    result["kalshi_markets_fetched"] = kalshi_inventory_diagnostics.get("kalshi_markets_fetched", len(kalshi_markets))
    result["kalshi_markets_retained"] = kalshi_inventory_diagnostics.get("kalshi_markets_retained", len(kalshi_markets))
    result["kalshi_markets_discarded"] = kalshi_inventory_diagnostics.get("kalshi_markets_discarded", 0)
    result["kalshi_inventory_discarded_reason_counts"] = kalshi_inventory_diagnostics.get("discarded_reason_counts", {})
    result["sportsbook_futures_fetched"] = len(sportsbook_futures)
    result["sportsbook_futures_retained"] = len([line for line in sportsbook_futures if line.get("market_type") in {"championship_winner", "conference_winner", "division_winner", "award", "season_award"}])
    if save:
        scan_run_id, _opportunity_ids = OpportunityStore(db_path).save_scan_result(result, scan_mode="scan-live-arb", filters={"sport": sport_key, "keyword": keyword, "category": category, "limit": limit, "region": region, "bookmaker": bookmaker, "min_edge": min_edge, "min_score": min_score, "max_close_hours": max_close_hours, "include_low_confidence": include_low_confidence})
        result["saved_scan_run_id"] = scan_run_id
    if as_json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print("Polylens Live Arbitrage Scan")
        print("=" * 29)
        print(f"Markets scanned by venue: {result['markets_scanned_by_venue']}")
        print(f"Matches found by venue pair: {result['matches_found_by_venue_pair']}")
        print(f"Arbitrage candidates found: {result['arbitrage_candidates_found']}")
        print(f"Candidates before/after filtering: {result.get('candidates_before_filtering', 0)}/{result.get('candidates_after_filtering', 0)}")
        print(f"Filter reasons: {result.get('filter_reasons', {})}")
        print("Skipped/rejected reasons:")
        for reason, count in result["skipped_rejected_reason_counts"].items():
            print(f"- {count}: {reason}")
        if not result["skipped_rejected_reason_counts"]:
            print("- none recorded")
        print("Top candidates:")
        for candidate in result["top_candidates"][:10]:
            edge = candidate.get("estimated_edge")
            edge_text = "insufficient pricing data" if edge is None else f"edge={edge:.4f}"
            print(f"- {candidate.get('venue_pair')} score={candidate.get('execution_score')} {candidate.get('confidence_band')} {edge_text}")
            print(f"  {candidate.get('polymarket_title') or candidate.get('kalshi_title')} <> {candidate.get('kalshi_title') or candidate.get('sportsbook')}")
            print(f"  {candidate.get('pricing_reason') or candidate.get('reason')}")
        if not result["top_candidates"]:
            print("- none found; see skipped/rejected reasons above")
    return result


def _env_int(name: str, default: int) -> int:
    value = os.environ.get(name)
    if value in (None, ""):
        return default
    try:
        return int(value)
    except ValueError:
        return default


def _env_float(name: str, default: float | None = None) -> float | None:
    value = os.environ.get(name)
    if value in (None, ""):
        return default
    try:
        return float(value)
    except ValueError:
        return default


def _env_str(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    return default if value in (None, "") else value


def debug_polymarket_search(keyword: str | None = None, sport_key: str | None = None, category: str | None = None, limit: int = 100, as_json: bool = False) -> dict[str, Any]:
    client = PolymarketClient(raw_dir="data/raw")
    diagnostics = client.debug_active_market_search(keyword=keyword, sport=sport_key, category=category, limit=limit)
    if as_json:
        print(json.dumps(diagnostics, indent=2, sort_keys=True))
    else:
        print("Polylens Polymarket Search Debug")
        print("=" * 33)
        print(f"Raw markets returned: {diagnostics['raw_markets_returned']}")
        print(f"Filtered markets retained: {diagnostics['filtered_markets_retained']}")
        print(f"Markets discarded: {diagnostics['markets_discarded']}")
        print("Discarded markets:")
        for item in diagnostics["discarded_markets"][:20]:
            print(f"- {item.get('title')} ({', '.join(item.get('discard_reasons') or [])})")
    return diagnostics


def debug_kalshi_inventory(limit: int = 100, as_json: bool = False) -> dict[str, Any]:
    client = KalshiClient(raw_dir="data/raw")
    markets = client.get_markets(status="open", limit=min(max(limit, 1), 1000), max_pages=5)
    _retained, diagnostics = filter_kalshi_inventory(markets)
    if as_json:
        print(json.dumps(diagnostics, indent=2, sort_keys=True))
    else:
        print("Polylens Kalshi Inventory Debug")
        print("=" * 31)
        print(f"Kalshi markets fetched: {diagnostics['kalshi_markets_fetched']}")
        print(f"Kalshi markets retained: {diagnostics['kalshi_markets_retained']}")
        print(f"Kalshi markets discarded: {diagnostics['kalshi_markets_discarded']}")
        print(f"Discarded reason counts: {diagnostics['discarded_reason_counts']}")
        print("Discarded sample:")
        for item in diagnostics["discarded_sample"][:20]:
            print(f"- {item.get('ticker')} {item.get('title')} ({item.get('discard_reason')})")
    return diagnostics


def explain_live_matches_cli(
    sport_key: str | None = None,
    keyword: str | None = None,
    limit: int = 100,
    as_json: bool = False,
    accepted_only: bool = False,
    rejected_only: bool = False,
) -> dict[str, Any]:
    logger = logging.getLogger(__name__)
    poly_client = PolymarketClient(raw_dir="data/raw")
    kalshi_client = KalshiClient(raw_dir="data/raw")
    try:
        polymarket_markets = poly_client.get_active_markets(keyword=keyword, sport=sport_key, limit=limit)
    except Exception as exc:
        logger.warning("Polymarket live discovery failed during match diagnostics: %s", exc)
        polymarket_markets = []
    kalshi_inventory_diagnostics: dict[str, Any] = {}
    try:
        kalshi_markets_raw = kalshi_client.get_markets(status="open", limit=min(max(limit, 1), 1000), max_pages=5)
        kalshi_markets, kalshi_inventory_diagnostics = filter_kalshi_inventory(kalshi_markets_raw)
    except Exception as exc:
        logger.warning("Kalshi live discovery failed during match diagnostics: %s", exc)
        kalshi_markets = []
    sportsbook_lines: list[dict[str, Any]] = []
    if sport_key:
        try:
            sportsbook_lines = fetch_odds(sport_key, markets="h2h,spreads,totals", quiet=True)
        except MissingOddsAPIKey:
            sportsbook_lines = []
        except Exception as exc:
            logger.warning("Sportsbook odds fetch failed during match diagnostics: %s", exc)
        try:
            futures_result = fetch_futures(sport_key, quiet=True)
            if futures_result.get("supported"):
                sportsbook_lines.extend(futures_result.get("futures") or [])
        except MissingOddsAPIKey:
            pass
        except Exception as exc:
            logger.warning("Sportsbook futures fetch failed during match diagnostics: %s", exc)
    diagnostics = explain_live_market_matches(polymarket_markets, kalshi_markets, sportsbook_lines, accepted_only=accepted_only, rejected_only=rejected_only)
    diagnostics["kalshi_inventory_filter"] = kalshi_inventory_diagnostics
    if as_json:
        print(json.dumps(diagnostics, indent=2, sort_keys=True))
    else:
        print("Polylens Live Match Diagnostics")
        print("=" * 32)
        print(f"Matches attempted: {diagnostics['matches_attempted']}")
        print(f"Matches accepted: {diagnostics['matches_accepted']}")
        print(f"Matches rejected: {diagnostics['matches_rejected']}")
        print(f"Top rejection reasons: {diagnostics['top_rejection_reasons']}")
        print("Sample rejected matches:")
        for item in diagnostics["rejected_matches_sample"][:20]:
            print(f"- {item.get('source_venue')} <> {item.get('target_venue')}: {item.get('rejection_reason')}")
            print(f"  {item.get('source_market_title')} <> {item.get('target_market_title')}")
            print(f"  parsed={item.get('parsed_fields')}")
    return diagnostics


def watch_live_arb(
    interval_seconds: int = 60,
    min_edge: float | None = None,
    min_score: float | None = None,
    max_close_hours: float | None = None,
    sport_key: str | None = None,
    keyword: str | None = None,
    category: str | None = None,
    bookmaker: str | None = None,
    region: str = "us",
    use_webhook: bool = False,
    once: bool = False,
    as_json: bool = False,
    save: bool = False,
    db_path: str = "data/polylens.db",
) -> dict[str, Any]:
    interval_seconds = interval_seconds if interval_seconds is not None else _env_int("POLYLENS_INTERVAL", 60)
    min_score = min_score if min_score is not None else _env_float("POLYLENS_MIN_SCORE")
    min_edge = min_edge if min_edge is not None else _env_float("POLYLENS_MIN_EDGE")
    db_path = db_path if db_path is not None else _env_str("POLYLENS_DB_PATH", "data/polylens.db")
    region = region or _env_str("POLYLENS_REGION", "us")
    bookmaker = bookmaker or _env_str("POLYLENS_BOOKMAKER")
    sport_key = sport_key or _env_str("POLYLENS_SPORT")
    keyword = keyword or _env_str("POLYLENS_KEYWORD")
    category = category or _env_str("POLYLENS_CATEGORY")
    notifier = build_notifier(use_webhook=use_webhook)
    result = watch_live_arbitrage(
        notifier,
        interval_seconds=interval_seconds,
        once=once,
        as_json=as_json,
        sport_key=sport_key,
        keyword=keyword,
        category=category,
        bookmaker=bookmaker,
        region=region,
        min_edge=min_edge,
        min_score=min_score,
        max_close_hours=max_close_hours,
        include_low_confidence=False,
        save=save,
        db_path=db_path,
    )
    if not as_json:
        print("Polylens Live Arbitrage Watch")
        print("=" * 31)
        print(f"Iterations: {result.get('iterations')}")
        print(f"Alerts sent: {result.get('alerts_sent')}")
        print(f"Duplicates suppressed: {result.get('duplicates_suppressed')}")
        scan = result.get("scan", {})
        print(f"Candidates after filtering: {scan.get('candidates_after_filtering', 0)}")
        print(f"Filter reasons: {scan.get('filter_reasons', {})}")
    return result


def recent_opportunities(limit: int = 20, db_path: str = "data/polylens.db", as_json: bool = False) -> list[dict[str, Any]]:
    rows = OpportunityStore(db_path).recent_opportunities(limit=limit)
    if as_json:
        print(json.dumps(rows, indent=2, sort_keys=True))
    else:
        for row in rows:
            titles = row.get("market_titles") or {}
            print(f"{row.get('id')} {row.get('timestamp')} {row.get('venue_pair')} edge={row.get('raw_edge')} score={row.get('execution_score')}: {titles.get('polymarket') or titles.get('kalshi')} <> {titles.get('kalshi') or titles.get('sportsbook') or titles.get('sportsbook_team')}")
    return rows


def recent_alerts(limit: int = 20, db_path: str = "data/polylens.db", as_json: bool = False) -> list[dict[str, Any]]:
    rows = OpportunityStore(db_path).recent_alerts(limit=limit)
    if as_json:
        print(json.dumps(rows, indent=2, sort_keys=True))
    else:
        for row in rows:
            print(f"{row.get('id')} {row.get('sent_timestamp')} {row.get('destination')} status={row.get('status')} opportunity={row.get('opportunity_id')}")
    return rows


def opportunity_stats(db_path: str = "data/polylens.db", as_json: bool = False) -> dict[str, Any]:
    stats = OpportunityStore(db_path).stats()
    if as_json:
        print(json.dumps(stats, indent=2, sort_keys=True))
    else:
        print(f"Scan runs: {stats['scan_runs']}")
        print(f"Opportunities: {stats['opportunities']}")
        print(f"Alerts: {stats['alerts']}")
        print(f"Rejected candidates: {stats['rejected_candidates']}")
        print(f"Top venue pairs: {stats['top_venue_pairs']}")
    return stats


def main() -> None:
    setup_logging()
    parser = argparse.ArgumentParser(prog="polylens")
    sub = parser.add_subparsers(dest="command", required=True)

    wallet_parser = sub.add_parser("analyze-wallet")
    wallet_parser.add_argument("wallet")

    export_parser = sub.add_parser("export-wallet")
    export_parser.add_argument("wallet")
    export_parser.add_argument("--include-kalshi", action="store_true", help="include conservative Polymarket/Kalshi market-overlap candidates")
    export_parser.add_argument("--include-pricing", action="store_true", help="include price-aware arbitrage calculations for Kalshi overlap candidates")

    compare_parser = sub.add_parser("compare-kalshi")
    compare_parser.add_argument("wallet")

    scan_parser = sub.add_parser("scan-arb")
    scan_parser.add_argument("wallet")

    explain_parser = sub.add_parser("explain-matches")
    explain_parser.add_argument("wallet")
    explain_parser.add_argument("--json", action="store_true", help="emit match diagnostics as JSON")
    explain_parser.add_argument("--save", action="store_true")
    explain_parser.add_argument("--db-path", default="data/polylens.db")

    inventory_parser = sub.add_parser("market-inventory")
    inventory_parser.add_argument("wallet")
    inventory_parser.add_argument("--include-closed", action="store_true", help="include closed/settled Kalshi markets when the API supports them")
    inventory_parser.add_argument("--json", action="store_true", help="emit market inventory as JSON")

    sports_parser = sub.add_parser("list-sportsbooks")
    sports_parser.add_argument("--json", action="store_true", help="emit sports list as JSON")

    odds_parser = sub.add_parser("fetch-odds")
    odds_parser.add_argument("--sport", required=True, dest="sport_key")
    odds_parser.add_argument("--bookmaker")
    odds_parser.add_argument("--region", default="us")
    odds_parser.add_argument("--markets", default="h2h,spreads,totals")
    odds_parser.add_argument("--json", action="store_true")

    futures_parser = sub.add_parser("fetch-futures")
    futures_parser.add_argument("--sport", required=True, dest="sport_key")
    futures_parser.add_argument("--bookmaker")
    futures_parser.add_argument("--region", default="us")
    futures_parser.add_argument("--json", action="store_true")

    sportsbook_parser = sub.add_parser("scan-sportsbook-arb")
    sportsbook_parser.add_argument("wallet")
    sportsbook_parser.add_argument("--sport", required=True, dest="sport_key")
    sportsbook_parser.add_argument("--bookmaker")
    sportsbook_parser.add_argument("--region", default="us")
    sportsbook_parser.add_argument("--json", action="store_true")

    debug_futures_parser = sub.add_parser("debug-futures-inventory")
    debug_futures_parser.add_argument("--sport", dest="sport_key", required=True)
    debug_futures_parser.add_argument("--bookmaker")
    debug_futures_parser.add_argument("--json", action="store_true")

    debug_field_parser = sub.add_parser("debug-synthetic-field")
    debug_field_parser.add_argument("--sport", dest="sport_key", required=True)
    debug_field_parser.add_argument("--team", dest="selected_team", required=True)
    debug_field_parser.add_argument("--json", action="store_true")

    scan_multibook_parser = sub.add_parser("scan-multibook-arb")
    scan_multibook_parser.add_argument("--sport", dest="sport_key")
    scan_multibook_parser.add_argument("--keyword")
    scan_multibook_parser.add_argument("--limit", type=int, default=100)
    scan_multibook_parser.add_argument("--bankroll", type=float)
    scan_multibook_parser.add_argument("--min-guaranteed-roi", type=float)
    scan_multibook_parser.add_argument("--json", action="store_true")

    find_hedges_parser = sub.add_parser("find-hedges")
    find_hedges_parser.add_argument("--keyword")
    find_hedges_parser.add_argument("--sport", dest="sport_key")
    find_hedges_parser.add_argument("--limit", type=int, default=100)
    find_hedges_parser.add_argument("--json", action="store_true")

    scan_true_parser = sub.add_parser("scan-true-arb")
    scan_true_parser.add_argument("--keyword")
    scan_true_parser.add_argument("--sport", dest="sport_key")
    scan_true_parser.add_argument("--limit", type=int, default=100)
    scan_true_parser.add_argument("--bankroll", type=float)
    scan_true_parser.add_argument("--min-guaranteed-roi", type=float)
    scan_true_parser.add_argument("--include-hedges", action="store_true")
    scan_true_parser.add_argument("--json", action="store_true")

    scan_live_parser = sub.add_parser("scan-live-arb")
    scan_live_parser.add_argument("--sport", dest="sport_key")
    scan_live_parser.add_argument("--keyword")
    scan_live_parser.add_argument("--category")
    scan_live_parser.add_argument("--limit", type=int, default=100)
    scan_live_parser.add_argument("--region", default="us")
    scan_live_parser.add_argument("--bookmaker")
    scan_live_parser.add_argument("--json", action="store_true")
    scan_live_parser.add_argument("--min-edge", type=float)
    scan_live_parser.add_argument("--min-score", type=float)
    scan_live_parser.add_argument("--max-close-hours", type=float)
    scan_live_parser.add_argument("--include-low-confidence", action="store_true")
    scan_live_parser.add_argument("--save", action="store_true")
    scan_live_parser.add_argument("--db-path", default="data/polylens.db")

    explain_live_parser = sub.add_parser("explain-live-matches")
    explain_live_parser.add_argument("--sport", dest="sport_key")
    explain_live_parser.add_argument("--keyword")
    explain_live_parser.add_argument("--limit", type=int, default=100)
    explain_live_parser.add_argument("--json", action="store_true")
    explain_live_parser.add_argument("--accepted-only", action="store_true")
    explain_live_parser.add_argument("--rejected-only", action="store_true")

    debug_poly_parser = sub.add_parser("debug-polymarket-search")
    debug_poly_parser.add_argument("--keyword")
    debug_poly_parser.add_argument("--sport", dest="sport_key")
    debug_poly_parser.add_argument("--category")
    debug_poly_parser.add_argument("--limit", type=int, default=100)
    debug_poly_parser.add_argument("--json", action="store_true")

    debug_kalshi_parser = sub.add_parser("debug-kalshi-inventory")
    debug_kalshi_parser.add_argument("--limit", type=int, default=100)
    debug_kalshi_parser.add_argument("--json", action="store_true")

    watch_parser = sub.add_parser("watch-live-arb")
    watch_parser.add_argument("--interval", type=int, dest="interval_seconds")
    watch_parser.add_argument("--min-edge", type=float)
    watch_parser.add_argument("--min-score", type=float)
    watch_parser.add_argument("--max-close-hours", type=float)
    watch_parser.add_argument("--sport", dest="sport_key")
    watch_parser.add_argument("--keyword")
    watch_parser.add_argument("--category")
    watch_parser.add_argument("--bookmaker")
    watch_parser.add_argument("--region")
    watch_parser.add_argument("--webhook", action="store_true", dest="use_webhook")
    watch_parser.add_argument("--once", action="store_true")
    watch_parser.add_argument("--json", action="store_true")
    watch_parser.add_argument("--save", action="store_true")
    watch_parser.add_argument("--db-path")

    recent_parser = sub.add_parser("recent-opportunities")
    recent_parser.add_argument("--limit", type=int, default=20)
    recent_parser.add_argument("--db-path", default="data/polylens.db")
    recent_parser.add_argument("--json", action="store_true")

    alerts_parser = sub.add_parser("recent-alerts")
    alerts_parser.add_argument("--limit", type=int, default=20)
    alerts_parser.add_argument("--db-path", default="data/polylens.db")
    alerts_parser.add_argument("--json", action="store_true")

    stats_parser = sub.add_parser("opportunity-stats")
    stats_parser.add_argument("--db-path", default="data/polylens.db")
    stats_parser.add_argument("--json", action="store_true")

    args = parser.parse_args()

    if args.command == "analyze-wallet":
        analyze_wallet(args.wallet)
    elif args.command == "export-wallet":
        export_wallet(args.wallet, include_kalshi=args.include_kalshi or args.include_pricing, include_pricing=args.include_pricing)
    elif args.command == "compare-kalshi":
        compare_kalshi(args.wallet)
    elif args.command == "scan-arb":
        scan_arb(args.wallet)
    elif args.command == "explain-matches":
        explain_matches(args.wallet, as_json=args.json, save=args.save, db_path=args.db_path)
    elif args.command == "market-inventory":
        market_inventory(args.wallet, include_closed=args.include_closed, as_json=args.json)
    elif args.command == "list-sportsbooks":
        list_sportsbooks(as_json=args.json)
    elif args.command == "fetch-odds":
        fetch_odds(args.sport_key, bookmaker=args.bookmaker, region=args.region, markets=args.markets, as_json=args.json)
    elif args.command == "fetch-futures":
        fetch_futures(args.sport_key, bookmaker=args.bookmaker, region=args.region, as_json=args.json)
    elif args.command == "scan-sportsbook-arb":
        scan_sportsbook_arb(args.wallet, args.sport_key, bookmaker=args.bookmaker, region=args.region, as_json=args.json)
    elif args.command == "scan-live-arb":
        scan_live_arb(sport_key=args.sport_key, keyword=args.keyword, category=args.category, limit=args.limit, region=args.region, bookmaker=args.bookmaker, as_json=args.json, min_edge=args.min_edge, min_score=args.min_score, max_close_hours=args.max_close_hours, include_low_confidence=args.include_low_confidence, save=args.save, db_path=args.db_path)
    elif args.command == "scan-true-arb":
        scan_true_arb(keyword=args.keyword, sport_key=args.sport_key, limit=args.limit, bankroll=args.bankroll, min_guaranteed_roi=args.min_guaranteed_roi, include_hedges=args.include_hedges, as_json=args.json)
    elif args.command == "scan-multibook-arb":
        scan_multibook_arb(sport_key=args.sport_key, keyword=args.keyword, limit=args.limit, bankroll=args.bankroll, min_guaranteed_roi=args.min_guaranteed_roi, as_json=args.json)
    elif args.command == "debug-synthetic-field":
        debug_synthetic_field_cli(sport_key=args.sport_key, selected_team=args.selected_team, as_json=args.json)
    elif args.command == "debug-futures-inventory":
        debug_futures_inventory(sport_key=args.sport_key, bookmaker=args.bookmaker, as_json=args.json)
    elif args.command == "find-hedges":
        find_hedges(keyword=args.keyword, sport_key=args.sport_key, limit=args.limit, as_json=args.json)
    elif args.command == "explain-live-matches":
        explain_live_matches_cli(sport_key=args.sport_key, keyword=args.keyword, limit=args.limit, as_json=args.json, accepted_only=args.accepted_only, rejected_only=args.rejected_only)
    elif args.command == "debug-polymarket-search":
        debug_polymarket_search(keyword=args.keyword, sport_key=args.sport_key, category=args.category, limit=args.limit, as_json=args.json)
    elif args.command == "debug-kalshi-inventory":
        debug_kalshi_inventory(limit=args.limit, as_json=args.json)
    elif args.command == "watch-live-arb":
        watch_live_arb(interval_seconds=args.interval_seconds, min_edge=args.min_edge, min_score=args.min_score, max_close_hours=args.max_close_hours, sport_key=args.sport_key, keyword=args.keyword, category=args.category, bookmaker=args.bookmaker, region=args.region, use_webhook=args.use_webhook, once=args.once, as_json=args.json, save=args.save, db_path=args.db_path)
    elif args.command == "recent-opportunities":
        recent_opportunities(limit=args.limit, db_path=args.db_path, as_json=args.json)
    elif args.command == "recent-alerts":
        recent_alerts(limit=args.limit, db_path=args.db_path, as_json=args.json)
    elif args.command == "opportunity-stats":
        opportunity_stats(db_path=args.db_path, as_json=args.json)


if __name__ == "__main__":
    try:
        main()
    except (MissingOddsAPIKey, MissingWebhookURLError) as exc:
        raise SystemExit(str(exc)) from exc
