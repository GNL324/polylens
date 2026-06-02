from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
from typing import Any

from src.adapters.kalshi import KalshiClient
from src.adapters.odds_api import MissingOddsAPIKey, OddsAPIClient
from src.adapters.polymarket import PolymarketClient
from src.analysis.arb_pricing import enrich_candidates_with_pricing, enrich_sportsbook_candidates_with_pricing
from src.analysis.arb_signals import detect_signals
from src.analysis.cross_market import compare_wallet_markets_to_kalshi
from src.analysis.live_arbitrage import scan_live_arbitrage
from src.analysis.market_inventory import summarize_market_inventory
from src.analysis.markets import summarize_markets
from src.analysis.match_diagnostics import explain_market_matches
from src.analysis.odds_normalization import normalize_odds_events
from src.analysis.pnl import summarize_pnl
from src.analysis.sportsbook_matching import match_sportsbook_lines
from src.analysis.timing import summarize_timing
from src.analysis.volume import summarize_volume
from src.reports.wallet_report import WalletReport


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


def explain_matches(wallet: str, as_json: bool = False) -> dict[str, Any]:
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


def scan_live_arb(
    sport_key: str | None = None,
    keyword: str | None = None,
    category: str | None = None,
    limit: int = 100,
    region: str = "us",
    bookmaker: str | None = None,
    as_json: bool = False,
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
    try:
        kalshi_markets = kalshi_client.get_markets(status="open", limit=min(max(limit, 1), 1000), max_pages=5)
    except Exception as exc:
        logger.warning("Kalshi live market discovery failed: %s", exc)
        kalshi_markets = []
        venue_errors["kalshi"] = f"Kalshi live discovery failed: {exc}"

    sportsbook_lines: list[dict[str, Any]] = []
    sportsbook_skipped_reason: str | None = None
    if sport_key:
        try:
            sportsbook_lines = fetch_odds(sport_key, bookmaker=bookmaker, region=region, markets="h2h,spreads,totals,outrights", quiet=True)
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
    )
    if as_json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print("Polylens Live Arbitrage Scan")
        print("=" * 29)
        print(f"Markets scanned by venue: {result['markets_scanned_by_venue']}")
        print(f"Matches found by venue pair: {result['matches_found_by_venue_pair']}")
        print(f"Arbitrage candidates found: {result['arbitrage_candidates_found']}")
        print("Skipped/rejected reasons:")
        for reason, count in result["skipped_rejected_reason_counts"].items():
            print(f"- {count}: {reason}")
        if not result["skipped_rejected_reason_counts"]:
            print("- none recorded")
        print("Top candidates:")
        for candidate in result["top_candidates"][:10]:
            edge = candidate.get("estimated_edge")
            edge_text = "insufficient pricing data" if edge is None else f"edge={edge:.4f}"
            print(f"- {candidate.get('venue_pair')} {candidate.get('confidence_band')} {edge_text}")
            print(f"  {candidate.get('polymarket_title') or candidate.get('kalshi_title')} <> {candidate.get('kalshi_title') or candidate.get('sportsbook')}")
            print(f"  {candidate.get('pricing_reason') or candidate.get('reason')}")
        if not result["top_candidates"]:
            print("- none found; see skipped/rejected reasons above")
    return result


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

    sportsbook_parser = sub.add_parser("scan-sportsbook-arb")
    sportsbook_parser.add_argument("wallet")
    sportsbook_parser.add_argument("--sport", required=True, dest="sport_key")
    sportsbook_parser.add_argument("--bookmaker")
    sportsbook_parser.add_argument("--region", default="us")
    sportsbook_parser.add_argument("--json", action="store_true")

    scan_live_parser = sub.add_parser("scan-live-arb")
    scan_live_parser.add_argument("--sport", dest="sport_key")
    scan_live_parser.add_argument("--keyword")
    scan_live_parser.add_argument("--category")
    scan_live_parser.add_argument("--limit", type=int, default=100)
    scan_live_parser.add_argument("--region", default="us")
    scan_live_parser.add_argument("--bookmaker")
    scan_live_parser.add_argument("--json", action="store_true")

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
        explain_matches(args.wallet, as_json=args.json)
    elif args.command == "market-inventory":
        market_inventory(args.wallet, include_closed=args.include_closed, as_json=args.json)
    elif args.command == "list-sportsbooks":
        list_sportsbooks(as_json=args.json)
    elif args.command == "fetch-odds":
        fetch_odds(args.sport_key, bookmaker=args.bookmaker, region=args.region, markets=args.markets, as_json=args.json)
    elif args.command == "scan-sportsbook-arb":
        scan_sportsbook_arb(args.wallet, args.sport_key, bookmaker=args.bookmaker, region=args.region, as_json=args.json)
    elif args.command == "scan-live-arb":
        scan_live_arb(sport_key=args.sport_key, keyword=args.keyword, category=args.category, limit=args.limit, region=args.region, bookmaker=args.bookmaker, as_json=args.json)


if __name__ == "__main__":
    try:
        main()
    except MissingOddsAPIKey as exc:
        raise SystemExit(str(exc)) from exc
