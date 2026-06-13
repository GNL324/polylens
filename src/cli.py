from __future__ import annotations

import argparse
import json
import logging
import os
LOGGER = logging.getLogger(__name__)
import resource
import time
from pathlib import Path
from typing import Any

from src.adapters.kalshi import KalshiAuthenticatedClient, KalshiAuthConfigError, KalshiClient
from src.adapters.oddsblaze import MissingOddsBlazeKey, OddsBlazeClient, OddsBlazeError
from src.adapters.odds_api import MissingOddsAPIKey, OddsAPIClient
from src.adapters.polymarket import PolymarketClient
from src.alerts.notifier import MissingWebhookURLError, build_notifier
from src.analysis.arb_pricing import enrich_candidates_with_pricing, enrich_sportsbook_candidates_with_pricing
from src.analysis.arb_signals import detect_signals
from src.analysis.cross_market import compare_wallet_markets_to_kalshi
from src.analysis.futures_inventory import summarize_futures_inventory
from src.analysis.hedge_leg_discovery import explain_hedge_search
from src.analysis.hedged_arbitrage import classify_arbitrage_candidates
from src.analysis.kalshi_account_analytics import build_kalshi_account_report, detect_kalshi_patterns, export_kalshi_report
from src.analysis.kalshi_account_history_export import DEFAULT_ACCOUNT_HISTORY_PATH, export_kalshi_account_history
from src.analysis.kalshi_backtest import run_kalshi_backtest, summarize_kalshi_backtests
from src.analysis.kalshi_strategy_simulator import SimulationConfig, compare_kalshi_strategies, export_kalshi_simulation, parse_csv_filter, parse_price_bands, simulate_kalshi_strategy
from src.analysis.kalshi_market_recorder import record_kalshi_markets, summarize_kalshi_market_data
from src.analysis.kalshi_inventory_filter import filter_kalshi_inventory
from src.analysis.live_arbitrage import scan_live_arbitrage
from src.analysis.live_match_diagnostics import explain_live_matches as explain_live_market_matches
from src.analysis.market_inventory import summarize_market_inventory
from src.analysis.multibook_arbitrage import scan_multibook_arbitrage
from src.analysis.markets import summarize_markets
from src.analysis.match_diagnostics import explain_market_matches
from src.analysis.odds_normalization import normalize_futures_events, normalize_odds_events
from src.analysis.opportunity_ranker import rank_opportunities
from src.analysis.pnl import summarize_pnl
from src.analysis.prop_arbitrage import scan_prop_arbitrage
from src.analysis.prop_normalization import normalize_player_props
from src.analysis.sportsbook_matching import match_sportsbook_lines
from src.analysis.synthetic_field import debug_synthetic_field as build_debug_synthetic_field
from src.analysis.timing import summarize_timing
from src.analysis.volume import summarize_volume
from src.analysis.wallet_activity import (
    DEFAULT_WALLET_ACTIVITY_DB,
    export_wallet_activity as build_wallet_activity_export,
    write_wallet_activity_export,
)
from src.analysis.wallet_forensics import build_wallet_forensics_report
from src.analysis.trader_discovery import discovery_report as build_trader_discovery_report
from src.analysis.trader_scanner import DEFAULT_WATCHLIST_PATH, scan_wallets as scan_trader_wallets
from src.analysis.trader_registry import (
    list_traders,
    top_traders,
    trader_summary,
    registry_stats,
    save_wallet_report,
    load_wallet_report,
    calculate_watch_score,
)
from src.analysis.watch_mode import watch_live_arbitrage
from src.reports.wallet_report import WalletReport
from src.storage.kalshi_market_data import DEFAULT_KALSHI_DATA_DB
from src.storage.opportunity_store import OpportunityStore
from src.storage.opportunity_analytics import opportunity_leaderboard as build_opportunity_leaderboard, opportunity_lifetimes as build_opportunity_lifetimes, opportunity_quality_report as build_opportunity_quality_report, record_scan_analytics
from src.storage.opportunities import load_recent_alerts as load_prop_recent_alerts, load_recent_opportunities as load_prop_recent_opportunities, opportunity_key as prop_opportunity_key, opportunity_stats as prop_opportunity_stats, record_alert_event, resolve_scanner_profile, save_alert as save_prop_alert, save_opportunity as save_prop_opportunity
from src.risk import RiskEngine
from src.trading.executor import KalshiExecutor
from src.trading.kalshi_strategy import scan_markets_for_signals
from src.trading.kalshi_live_smoke import run_kalshi_live_smoke_test
from src.trading.risk import RiskConfig
from src.notifications.telegram import send_telegram_alert


def _memory_mb() -> float:
    usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # Linux reports kilobytes; macOS reports bytes. Predix is Linux, but keep this portable.
    return round(usage / 1024 if usage > 10_000_000 else usage / 1024, 2)


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


def wallet_forensics_cli(wallet: str | None = None, input_json: str | None = None, as_json: bool = False) -> dict[str, Any]:
    logger = logging.getLogger(__name__)
    report = None
    try:
        if not input_json:
            raise ValueError("--input-json is required for offline wallet forensics")
        report = build_wallet_forensics_report(input_json, wallet=wallet)
        payload = report.to_dict()
        payload["accepted"] = True
    except Exception as exc:
        logger.warning("wallet forensics failed: %s", exc)
        payload = {"accepted": False, "error": str(exc)}
        if wallet:
            payload["wallet"] = wallet
    if as_json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    elif payload.get("accepted") and report is not None:
        print(report.summary_text())
    else:
        print(f"Wallet forensics error: {payload.get('error')}")
    return payload


def export_wallet_activity_cli(
    wallet: str,
    output: str | None = None,
    limit: int | None = None,
    as_json: bool = False,
    db_path: str = DEFAULT_WALLET_ACTIVITY_DB,
) -> dict[str, Any]:
    export = build_wallet_activity_export(wallet=wallet, limit=limit, db_path=db_path, store=True)
    payload = export.to_dict()
    if output:
        write_wallet_activity_export(export, output)
    if as_json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(output if output else f"wallet={payload['wallet']} events={payload['event_count']} source={payload['source']}")
    return payload


def analyze_trader_cli(
    wallet: str,
    limit: int | None = None,
    as_json: bool = False,
    output: str | None = None,
    db_path: str = DEFAULT_WALLET_ACTIVITY_DB,
    traders_db_path: str = "data/traders.db",
) -> dict[str, Any]:
    activity_export = build_wallet_activity_export(wallet=wallet, limit=limit, db_path=db_path, store=True)
    output_path = output or f"data/wallets/{wallet.lower()}_activity.json"
    write_wallet_activity_export(activity_export, output_path)
    report = build_wallet_forensics_report(output_path, wallet=wallet)
    report_payload = report.to_dict()
    watch_score = calculate_watch_score(report.classification, report.confidence, report.metrics, report.signals)
    report_payload["watch_score"] = watch_score
    save_wallet_report(report_payload, watch_score=watch_score, db_path=traders_db_path)
    result = {
        "wallet": report.wallet,
        "classification": report.classification,
        "confidence": report.confidence,
        "watch_score": watch_score,
        "event_count": activity_export.event_count,
        "activity_export": output_path,
    }
    print(json.dumps(result, indent=2, sort_keys=True) if as_json else json.dumps(result, indent=2, sort_keys=True))
    return result


def scan_top_traders_cli(
    wallet: str | None = None,
    watchlist: str = DEFAULT_WATCHLIST_PATH,
    limit: int | None = None,
    as_json: bool = False,
) -> dict[str, Any]:
    result = scan_trader_wallets(
        wallets=[wallet] if wallet else None,
        watchlist=watchlist,
        limit=limit,
        include_registry=wallet is None,
    )
    if as_json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(json.dumps(result, indent=2, sort_keys=True))
    return result


def discover_traders_cli(
    wallet: str | None = None,
    activity_export: str | None = None,
    watchlist: str = DEFAULT_WATCHLIST_PATH,
    limit: int | None = None,
    scan: bool = False,
    as_json: bool = False,
) -> dict[str, Any]:
    result = build_trader_discovery_report(
        wallet=wallet,
        activity_export=activity_export,
        watchlist=watchlist,
        limit=limit,
        scan=scan,
    )
    if as_json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(json.dumps(result, indent=2, sort_keys=True))
    return result


def trader_registry_summary_cli(
    classification: str | None = None,
    min_watch_score: int = 0,
    limit: int = 100,
    as_json: bool = False,
    db_path: str = "data/traders.db",
) -> dict[str, Any]:
    from src.analysis.trader_registry import registry_stats, list_traders
    
    stats = registry_stats(db_path)
    traders = list_traders(
        classification=classification,
        min_watch_score=min_watch_score,
        limit=limit,
        db_path=db_path,
    )
    
    result = {
        "total_traders": stats["total_traders"],
        "by_classification": stats["by_classification"],
        "top_watch_scores": stats["top_watch_scores"],
        "traders": [t.to_dict() for t in traders],
    }
    
    if as_json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(f"Trader Registry Summary")
        print(f"========================")
        print(f"Total Traders: {stats['total_traders']}")
        print(f"")
        print(f"By Classification:")
        for c in stats["by_classification"]:
            print(f"  {c['classification']}: {c['count']} traders (avg watch score: {c['avg_watch_score']})")
        print(f"")
        print(f"Top Watch Scores:")
        for t in stats["top_watch_scores"]:
            print(f"  {t['wallet'][:12]}... ({t['classification']}): {t['watch_score']}")
    return result


def trader_leaderboard_cli(
    limit: int = 20,
    classification: str | None = None,
    as_json: bool = False,
    db_path: str = "data/traders.db",
) -> dict[str, Any]:
    from src.analysis.trader_registry import top_traders
    
    traders = top_traders(limit=limit, classification=classification, db_path=db_path)
    
    result = {
        "traders": [t.to_dict() for t in traders],
    }
    
    if as_json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        title = "Trader Leaderboard" + (f" ({classification})" if classification else "")
        print(title)
        print("=" * len(title))
        for i, t in enumerate(traders, 1):
            print(f"{i:3d}. {t.wallet} | {t.classification} | confidence={t.confidence:.2f} | watch_score={t.watch_score}")
    return result


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


def kalshi_markets_cli(limit: int = 20, as_json: bool = False) -> list[dict[str, Any]]:
    markets = KalshiClient(raw_dir="data/raw").get_markets(status="open", limit=min(max(limit, 1), 1000), max_pages=1)
    rows = markets[:limit]
    if as_json:
        print(json.dumps(rows, indent=2, sort_keys=True))
    else:
        for market in rows:
            ident = market.get("market_identity") or {}
            pricing = market.get("pricing") or {}
            print(f"{ident.get('ticker') or market.get('ticker')} {ident.get('title') or market.get('title')} yes_ask={pricing.get('yes_ask')}")
    return rows


def kalshi_orderbook_cli(ticker: str, as_json: bool = False) -> dict[str, Any]:
    orderbook = KalshiClient(raw_dir="data/raw").get_orderbook(ticker)
    if as_json:
        print(json.dumps(orderbook, indent=2, sort_keys=True))
    else:
        print(json.dumps(orderbook, indent=2, sort_keys=True))
    return orderbook


def kalshi_paper_scan(limit: int = 20, max_price: float = 0.5, as_json: bool = False) -> list[dict[str, Any]]:
    markets = KalshiClient(raw_dir="data/raw").get_markets(status="open", limit=min(max(limit, 1), 1000), max_pages=1)
    signals = scan_markets_for_signals(markets, max_price=max_price, limit=limit)
    if as_json:
        print(json.dumps(signals, indent=2, sort_keys=True))
    else:
        for signal in signals:
            print(f"{signal['ticker']} {signal['side']} {signal['count']} @ {signal['price']} - {signal['reason']}")
    return signals


def kalshi_paper_trade(ticker: str, side: str, price: float, count: int, as_json: bool = False) -> dict[str, Any]:
    result = KalshiExecutor(RiskConfig.from_env()).submit_order(ticker, side, price, count)
    if as_json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(json.dumps(result, indent=2, sort_keys=True))
    return result


def kalshi_live_smoke_test(ticker: str, side: str, price: float, count: int, max_notional: float = 1.0, as_json: bool = False) -> dict[str, Any]:
    result = run_kalshi_live_smoke_test(ticker=ticker, side=side, price=price, count=count, max_notional=max_notional)
    if as_json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(json.dumps(result, indent=2, sort_keys=True))
    return result


def kalshi_status(as_json: bool = False) -> dict[str, Any]:
    config = RiskConfig.from_env()
    status = {
        "live_trading": config.live_trading,
        "dry_run": config.dry_run,
        "paper_mode": not (config.live_trading and not config.dry_run),
        "max_trade_dollars": config.max_trade_dollars,
        "max_open_exposure": config.max_open_exposure,
        "max_daily_loss": config.max_daily_loss,
        "duplicate_signal_cooldown_seconds": config.duplicate_signal_cooldown_seconds,
    }
    if as_json:
        print(json.dumps(status, indent=2, sort_keys=True))
    else:
        print(json.dumps(status, indent=2, sort_keys=True))
    return status


def _print_kalshi_auth_result(action: str, as_json: bool, **kwargs: Any) -> dict[str, Any]:
    try:
        client = KalshiAuthenticatedClient(raw_dir="data/raw")
        result = getattr(client, action)(**kwargs)
    except (KalshiAuthConfigError, Exception) as exc:
        result = {"accepted": False, "mode": "auth_error", "reason": str(exc)}
    if as_json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(json.dumps(result, indent=2, sort_keys=True))
    return result


def kalshi_account(as_json: bool = False) -> dict[str, Any]:
    return _print_kalshi_auth_result("get_account", as_json)


def kalshi_balance(as_json: bool = False) -> dict[str, Any]:
    return _print_kalshi_auth_result("get_balance", as_json)


def kalshi_positions(limit: int = 100, as_json: bool = False) -> dict[str, Any]:
    return _print_kalshi_auth_result("get_positions", as_json, limit=limit)


def kalshi_orders(limit: int = 100, as_json: bool = False) -> dict[str, Any]:
    return _print_kalshi_auth_result("get_orders", as_json, limit=limit)


def build_kalshi_read_only_report() -> dict[str, Any]:
    client = KalshiAuthenticatedClient(raw_dir="data/raw")
    balance = client.get_balance()
    positions = client.get_positions()
    orders = client.get_orders()
    try:
        fills = client.get_fills()
    except Exception as exc:
        fills = {"fills": [], "error": str(exc)}
    return build_kalshi_account_report(balance, positions, orders, fills)


def kalshi_report(as_json: bool = False) -> dict[str, Any]:
    try:
        report = build_kalshi_read_only_report()
    except (KalshiAuthConfigError, Exception) as exc:
        report = {"accepted": False, "mode": "auth_error", "reason": str(exc)}
    if as_json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        summary = report.get("summary") if isinstance(report, dict) else None
        if not summary:
            print(json.dumps(report, indent=2, sort_keys=True))
        else:
            print("Kalshi Account Report")
            print("=====================")
            print(f"Balance: {summary.get('account_balance')}")
            print(f"Open positions: {summary.get('open_position_count')}")
            print(f"Realized PnL: {summary.get('realized_pnl')}")
            print(f"Fees paid: {summary.get('fees_paid')}")
            print(f"Trade count: {summary.get('trade_count')}")
            print(f"Win rate: {summary.get('win_rate')}")
            print(f"Average entry price: {summary.get('average_entry_price')}")
            print(f"Average contract size: {summary.get('average_contract_size')}")
            print(f"Top market types: {report.get('trades_by_market_type')}")
            print(f"Top assets: {report.get('trades_by_asset')}")
            print(f"Behavior: {(report.get('patterns') or {}).get('behavior_classification')}")
    return report


def kalshi_export(as_json: bool = False) -> dict[str, Any]:
    try:
        report = build_kalshi_read_only_report()
        result = {"accepted": True, "files": export_kalshi_report(report)}
    except (KalshiAuthConfigError, Exception) as exc:
        result = {"accepted": False, "mode": "auth_error", "reason": str(exc)}
    if as_json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(json.dumps(result, indent=2, sort_keys=True))
    return result


def kalshi_export_account_history(output: str = DEFAULT_ACCOUNT_HISTORY_PATH, as_json: bool = False) -> dict[str, Any]:
    try:
        result = export_kalshi_account_history(KalshiAuthenticatedClient(), output)
    except KalshiAuthConfigError as exc:
        result = {"accepted": False, "mode": "auth_error", "reason": str(exc)}
    if as_json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(result.get("output") or result.get("reason"))
    return result


def kalshi_patterns(as_json: bool = False) -> dict[str, Any]:
    try:
        report = build_kalshi_read_only_report()
        patterns = detect_kalshi_patterns(report)
    except (KalshiAuthConfigError, Exception) as exc:
        patterns = {"accepted": False, "mode": "auth_error", "reason": str(exc)}
    if as_json:
        print(json.dumps(patterns, indent=2, sort_keys=True))
    else:
        print(json.dumps(patterns, indent=2, sort_keys=True))
    return patterns


def kalshi_simulate(assets: str | None = None, market_types: str | None = None, price_bands: str | None = None, max_contracts: int = 1, bankroll: float = 1000.0, fee_assumption: float = 0.0, strategy_mode: str = "extreme-probability", export: bool = False, as_json: bool = False) -> dict[str, Any]:
    try:
        client = KalshiAuthenticatedClient(raw_dir="data/raw")
        balance = client.get_balance()
        positions = client.get_positions()
        orders = client.get_orders()
        try:
            fills = client.get_fills()
        except Exception as exc:
            fills = {"fills": [], "error": str(exc)}
        config = SimulationConfig(assets=parse_csv_filter(assets), market_types=parse_csv_filter(market_types), price_bands=parse_price_bands(price_bands), max_contracts=max_contracts, bankroll=bankroll, fee_assumption=fee_assumption, strategy_mode=strategy_mode)
        result = simulate_kalshi_strategy(balance, positions, orders, fills, config)
        result["strategy_comparison"] = compare_kalshi_strategies(balance, positions, orders, fills, config)
        if export:
            result["exported_files"] = export_kalshi_simulation(result)
    except (KalshiAuthConfigError, Exception) as exc:
        result = {"accepted": False, "mode": "auth_error", "reason": str(exc)}
    if as_json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        summary = result.get("summary") if isinstance(result, dict) else None
        if not summary:
            print(json.dumps(result, indent=2, sort_keys=True))
        else:
            print("Kalshi Strategy Simulation")
            print("==========================")
            print(f"Mode: {summary.get('strategy_mode')}")
            print(f"Simulated trades: {summary.get('simulated_trades')}")
            print(f"Simulated PnL: {summary.get('simulated_pnl')}")
            print(f"Fees: {summary.get('fees')}")
            print(f"Net PnL: {summary.get('net_pnl')}")
            print(f"Win/Loss: {summary.get('win_count')}/{summary.get('loss_count')}")
            print(f"Max drawdown: {summary.get('max_drawdown')}")
            print(f"Average trade size: {summary.get('average_trade_size')}")
            print(f"Classification: {summary.get('strategy_classification')}")
            print(f"Enough data to automate safely: {summary.get('enough_data_to_automate_safely')}")
    return result


def kalshi_backtest(db_path: str = DEFAULT_KALSHI_DATA_DB, strategy: str = "all", fee_assumption: float = 0.0, spread_threshold: float = 0.05, bankroll: float = 1000.0, export: bool = False, as_json: bool = False) -> dict[str, Any]:
    try:
        result = run_kalshi_backtest(db_path=db_path, strategy=strategy, fee_assumption=fee_assumption, spread_threshold=spread_threshold, bankroll=bankroll, export=export)
    except Exception as exc:
        result = {"accepted": False, "mode": "backtest_error", "reason": str(exc)}
    if as_json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        if result.get("strategies"):
            print("Kalshi Backtest")
            print("===============")
            print(f"Data points: {(result.get('data') or {}).get('price_points')}")
            for row in result["strategies"]:
                print(f"{row['strategy']}: trades={row['trade_count']} net_pnl={row['net_pnl']} win_rate={row['win_rate']} drawdown={row['max_drawdown']}")
            print(f"Best strategy: {(result.get('summary') or {}).get('best_strategy')}")
        else:
            print(json.dumps(result, indent=2, sort_keys=True))
    return result


def kalshi_backtest_summary(db_path: str = DEFAULT_KALSHI_DATA_DB, as_json: bool = False) -> dict[str, Any]:
    try:
        result = summarize_kalshi_backtests(db_path=db_path)
    except Exception as exc:
        result = {"accepted": False, "mode": "backtest_error", "reason": str(exc)}
    if as_json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(json.dumps(result, indent=2, sort_keys=True))
    return result


def kalshi_record_markets(assets: str | None = None, market_types: str | None = None, interval: int = 60, duration_minutes: float | None = None, limit: int = 100, discovery_limit: int = 1000, event_ticker_prefix: str | None = None, ticker_prefix: str | None = None, db_path: str = DEFAULT_KALSHI_DATA_DB, as_json: bool = False) -> dict[str, Any]:
    try:
        result = record_kalshi_markets(KalshiClient(raw_dir="data/raw"), assets=parse_csv_filter(assets), market_types=parse_csv_filter(market_types), interval=interval, duration_minutes=duration_minutes, limit=limit, discovery_limit=discovery_limit, event_ticker_prefix=event_ticker_prefix, ticker_prefix=ticker_prefix, db_path=db_path)
    except Exception as exc:
        result = {"accepted": False, "mode": "record_error", "reason": str(exc)}
    if as_json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(json.dumps(result, indent=2, sort_keys=True))
    return result


def kalshi_data_summary(db_path: str = DEFAULT_KALSHI_DATA_DB, as_json: bool = False) -> dict[str, Any]:
    result = summarize_kalshi_market_data(db_path)
    if as_json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(json.dumps(result, indent=2, sort_keys=True))
    return result


def opportunity_ranker(
    venue: str | None = None,
    market_type: str | None = None,
    asset: str | None = None,
    sport: str | None = None,
    min_roi: float | None = None,
    min_confidence: float | None = None,
    max_age_seconds: int | None = None,
    limit: int = 20,
    export: bool = False,
    as_json: bool = False,
) -> dict[str, Any]:
    result = rank_opportunities(
        venue=venue,
        market_type=market_type,
        asset=asset,
        sport=sport,
        min_roi=min_roi,
        min_confidence=min_confidence,
        max_age_seconds=max_age_seconds,
        limit=limit,
        export=export,
    )
    if as_json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        summary = result.get("summary") or {}
        print("Opportunity Rankings")
        print("====================")
        print(f"Status: {summary.get('status')}")
        print(f"Best category: {summary.get('best_opportunity_category')}")
        print(f"Real trading decision ready: {summary.get('real_trading_decision_ready')}")
        for row in result.get("ranked_opportunities", []):
            print(
                f"{row.get('rank')}. {row.get('venue_pair')} {row.get('market_type')} "
                f"EV={row.get('expected_value')} ROI={row.get('estimated_roi')} "
                f"score={row.get('ranking_score')} confidence={row.get('confidence_score')}"
            )
            print(f"   risk: {row.get('risk_notes')}")
        warnings = summary.get("data_quality_warnings") or []
        if warnings:
            print("Warnings:")
            for warning in warnings:
                print(f"- {warning}")
        if result.get("files"):
            print(f"Saved JSON report: {result['files'].get('json')}")
            print(f"Saved CSV report: {result['files'].get('csv')}")
    return result


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


def fetch_player_props(sport_key: str, event_id: str | None = None, bookmaker: str | None = None, region: str = "us", markets: str | None = None, as_json: bool = False, quiet: bool = False) -> list[dict[str, Any]]:
    client = OddsAPIClient(raw_dir="data/raw")
    payload = client.get_player_props(sport_key, event_id=event_id, regions=region, markets=markets, bookmakers=bookmaker)
    raw_events = payload.get("events") if isinstance(payload, dict) else payload
    logger = logging.getLogger(__name__)
    logger.info("player prop normalization starting events=%s memory_mb=%s", len(raw_events or []), _memory_mb())
    normalized = normalize_player_props(raw_events or [])
    logger.info("player prop normalization complete props=%s memory_mb=%s", len(normalized), _memory_mb())
    diagnostics = _player_prop_diagnostics(payload, normalized)
    output = {"props": normalized, "diagnostics": diagnostics}
    if as_json:
        print(json.dumps(output, indent=2, sort_keys=True))
    elif not quiet:
        print(f"Fetched player props: {len(normalized)}")
        print(f"Events discovered/scanned/failed: {diagnostics['events_discovered']}/{diagnostics['events_scanned']}/{diagnostics['events_failed']}")
        for row in normalized[:20]:
            print(f"- {row.get('player')} {row.get('market_type')} {row.get('side')} {row.get('line')} {row.get('bookmaker')} odds={row.get('odds')}")
    return normalized


DEFAULT_ODDSBLAZE_SPORTSBOOKS = ("draftkings", "fanduel", "betmgm", "caesars", "hard-rock")


def fetch_oddsblaze_odds(
    sportsbook: str,
    league: str,
    market: str | None = None,
    market_contains: str | None = None,
    main: bool | None = None,
    live: bool | None = None,
    as_json: bool = False,
    quiet: bool = False,
) -> list[dict[str, Any]]:
    client = OddsBlazeClient(raw_dir="data/raw")
    try:
        rows = client.fetch_odds(
            sportsbook=sportsbook,
            league=_oddsblaze_league(league),
            market=market,
            market_contains=market_contains,
            main=main,
            live=live,
        )
    except OddsBlazeError as exc:
        LOGGER.warning("OddsBlaze fetch failed for %s/%s: %s", league, sportsbook, exc)
        rows = []
    if as_json:
        print(json.dumps(rows, indent=2, sort_keys=True))
    elif not quiet and rows:
        print(f"Fetched OddsBlaze odds: {len(rows)}")
        for row in rows[:20]:
            print(f"- {row.get("player")} {row.get("market_type")} {row.get("side")} {row.get("line")} {row.get("sportsbook")} odds={row.get("odds")}")
    return rows




def fetch_provider_player_props(
    sport_key: str,
    event_id: str | None = None,
    bookmaker: str | None = None,
    region: str = "us",
    markets: str | None = None,
    provider: str = "odds-api",
    oddsblaze_sportsbooks: list[str] | None = None,
    oddsblaze_market_contains: str | None = None,
) -> list[dict[str, Any]]:
    provider_key = (provider or "odds-api").strip().lower()
    if provider_key not in {"odds-api", "oddsblaze", "all"}:
        raise ValueError("--provider must be one of odds-api, oddsblaze, all")
    props: list[dict[str, Any]] = []
    odd_api_failed = False
    if provider_key in {"odds-api", "all"}:
        try:
            props.extend(fetch_player_props(sport_key, event_id=event_id, bookmaker=bookmaker, region=region, markets=markets, quiet=True))
        except Exception as exc:
            LOGGER.warning("fetch_player_props (odds-api) failed: %s", exc)
            odd_api_failed = True
    if provider_key in {"oddsblaze", "all"}:
        league = _oddsblaze_league(sport_key)
        market_contains = _oddsblaze_market_contains(markets, oddsblaze_market_contains)
        any_success = False
        for sportsbook in oddsblaze_sportsbooks or list(DEFAULT_ODDSBLAZE_SPORTSBOOKS):
            try:
                rows = fetch_oddsblaze_odds(sportsbook=sportsbook, league=league, market_contains=market_contains, main=True, live=False, quiet=True)
                if rows:
                    any_success = True
                props.extend(rows)
            except Exception as exc:
                LOGGER.warning("OddsBlaze fetch failed for sportsbook %s: %s", sportsbook, exc)
        if not any_success:
            LOGGER.warning("All OddsBlaze sportsbooks failed or returned no data")
    if provider_key == "odds-api" and odd_api_failed:
        raise RuntimeError("odds-api provider failed and no fallback providers configured")
    if provider_key == "oddsblaze" and not props:
        raise RuntimeError("All OddsBlaze sportsbooks failed")
    return props


def _split_csv_values(values: list[str] | str | None) -> list[str]:
    if not values:
        return []
    parts = values if isinstance(values, list) else [values]
    return [item.strip() for part in parts for item in str(part).split(",") if item.strip()]


def _parse_bool(value: str | bool | None) -> bool | None:
    if value is None or isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y"}:
        return True
    if text in {"0", "false", "no", "n"}:
        return False
    raise ValueError(f"invalid boolean value: {value}")


def _oddsblaze_league(value: str | None) -> str:
    mapping = {
        "basketball_nba": "nba",
        "americanfootball_nfl": "nfl",
        "baseball_mlb": "mlb",
        "icehockey_nhl": "nhl",
    }
    text = str(value or "").strip()
    return mapping.get(text, text).lower()


def _oddsblaze_market_contains(markets: str | None, override: str | None = None) -> str:
    if override:
        return override
    internal_player_markets = {
        "player_points",
        "player_rebounds",
        "player_assists",
        "player_threes",
        "player_blocks",
        "player_steals",
        "player_turnovers",
        "player_points_rebounds_assists",
        "player_points_rebounds",
        "player_points_assists",
        "player_rebounds_assists",
    }
    requested = {item.strip().lower() for item in str(markets or "").split(",") if item.strip()}
    if not requested or requested & internal_player_markets:
        return "Player"
    return str(markets).strip()


def debug_player_props(sport_key: str, event_id: str | None = None, bookmaker: str | None = None, region: str = "us", markets: str | None = None, as_json: bool = False) -> dict[str, Any]:
    client = OddsAPIClient(raw_dir="data/raw")
    payload = client.get_player_props(sport_key, event_id=event_id, regions=region, markets=markets, bookmakers=bookmaker)
    raw_events = payload.get("events") if isinstance(payload, dict) else payload
    logger = logging.getLogger(__name__)
    logger.info("player prop debug normalization starting events=%s memory_mb=%s", len(raw_events or []), _memory_mb())
    normalized = normalize_player_props(raw_events or [])
    logger.info("player prop debug normalization complete props=%s memory_mb=%s", len(normalized), _memory_mb())
    diagnostics = _player_prop_diagnostics(payload, normalized)
    event_rows = []
    for event in raw_events or []:
        markets_seen = []
        prop_count = 0
        for book in event.get("bookmakers", []) or []:
            for market in book.get("markets", []) or []:
                markets_seen.append(market.get("key"))
                prop_count += len(market.get("outcomes", []) or [])
        event_rows.append({"event_id": event.get("id"), "home_team": event.get("home_team"), "away_team": event.get("away_team"), "available_prop_markets": sorted(set(markets_seen)), "prop_count": prop_count})
    result = {"events": event_rows, "diagnostics": diagnostics}
    if as_json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print("Polylens Player Props Debug")
        print("=" * 28)
        for event in event_rows:
            print(f"{event.get('event_id')} {event.get('home_team')} vs {event.get('away_team')} markets={event.get('available_prop_markets')} props={event.get('prop_count')}")
        print(f"Rejected markets: {diagnostics['prop_markets_rejected']}")
    return result


def _player_prop_diagnostics(payload: Any, normalized: list[dict[str, Any]]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {"events_discovered": 0, "events_scanned": 0, "events_failed": 0, "prop_markets_supported": sorted({row.get('market_type') for row in normalized if row.get('market_type')}), "prop_markets_rejected": [], "props_normalized": len(normalized)}
    return {"events_discovered": payload.get("events_discovered", 0), "events_scanned": payload.get("events_scanned", 0), "events_failed": payload.get("events_failed", 0), "prop_markets_supported": payload.get("prop_markets_supported", []), "prop_markets_rejected": payload.get("prop_markets_rejected", []), "props_normalized": len(normalized)}


def scan_prop_arb(
    sport_key: str | None,
    event_id: str | None = None,
    bookmaker: str | None = None,
    region: str = "us",
    markets: str | None = None,
    provider: str = "odds-api",
    oddsblaze_sportsbooks: list[str] | None = None,
    oddsblaze_market_contains: str | None = None,
    bankroll: float | None = None,
    min_guaranteed_roi: float | None = None,
    min_profit: float | None = None,
    max_leg_age_seconds: float | None = None,
    max_cross_leg_update_gap_seconds: float | None = None,
    as_json: bool = False,
    profile: str | None = None,
    sportsbooks: str | None = None,
    db_path: str = "data/opportunities.db",
    record_analytics: bool = False,
    inactive_after_seconds: int = 900,
    summary_json: bool = False,
) -> dict[str, Any]:
    profile_row = resolve_scanner_profile(profile, db_path=db_path) if profile else None
    if profile and profile_row is None:
        raise ValueError("scanner profile not found")
    profile_sportsbooks = profile_row.get("sportsbooks") if profile_row else []
    sportsbook_filter = [item.strip().lower() for item in (sportsbooks.split(",") if sportsbooks else profile_sportsbooks or []) if str(item).strip()]
    if sportsbook_filter and any(item in {"all", "__all__", "*"} for item in sportsbook_filter):
        sportsbook_filter = []
    effective_sport = sport_key or (profile_row.get("sport") if profile_row else None)
    if not effective_sport:
        raise ValueError("--sport is required when no scanner profile supplies one")
    effective_markets = markets or (",".join(profile_row.get("markets") or []) if profile_row else None)
    effective_bookmaker = bookmaker or sportsbooks or (",".join(sportbook for sportbook in sportsbook_filter) if sportsbook_filter else None)
    effective_bankroll = bankroll if bankroll is not None else (profile_row.get("bankroll") if profile_row else None)
    effective_min_roi = min_guaranteed_roi if min_guaranteed_roi is not None else (profile_row.get("min_roi") if profile_row else None)
    started = time.time()
    props = fetch_provider_player_props(
        effective_sport,
        event_id=event_id,
        bookmaker=effective_bookmaker,
        region=region,
        markets=effective_markets,
        provider=provider,
        oddsblaze_sportsbooks=oddsblaze_sportsbooks or _split_csv_values(sportsbooks),
        oddsblaze_market_contains=oddsblaze_market_contains,
    )
    if sportsbook_filter:
        allowed = set(sportsbook_filter)
        props = [
            row for row in props
            if str(row.get("bookmaker_key") or row.get("bookmaker") or row.get("bookmaker_name") or "").strip().lower() in allowed
        ]
    duration = round(time.time() - started, 4)
    result = scan_prop_arbitrage(
        props,
        bankroll=effective_bankroll,
        min_guaranteed_roi=effective_min_roi,
        min_profit=min_profit,
        scan_duration_seconds=duration,
        api_calls=None,
        max_leg_age_seconds=max_leg_age_seconds,
        max_cross_leg_update_gap_seconds=max_cross_leg_update_gap_seconds,
    )
    if profile_row:
        result["scanner_profile"] = profile_row.get("name")
        result["scanner_profile_id"] = profile_row.get("id")
    if sportsbook_filter:
        result["sportsbooks_filter"] = sportsbook_filter
    result["provider"] = provider
    if record_analytics:
        result["analytics"] = record_scan_analytics(result, db_path=db_path, inactive_after_seconds=inactive_after_seconds)
    if summary_json:
        print(json.dumps(scan_prop_arb_summary(result), indent=2, sort_keys=True))
    elif as_json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print("Polylens Player Prop Arbitrage")
        print("=" * 31)
        print(f"Props fetched: {result['props_fetched']}")
        print(f"Matched prop pairs: {result['matched_prop_pairs']}")
        print(f"True arb candidates: {len(result['prop_arbitrage_candidates'])}")
        print(f"Rejection reasons: {result['rejection_reasons']}")
    return result


def scan_prop_arb_summary(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "props_fetched": result.get("props_fetched"),
        "normalized_props": result.get("normalized_props"),
        "matched_prop_pairs": result.get("matched_prop_pairs"),
        "true_arb_candidates": len(result.get("prop_arbitrage_candidates") or []),
        "rejection_reasons": result.get("rejection_reasons"),
        "provider": result.get("provider"),
        "sportsbooks_filter": result.get("sportsbooks_filter"),
        "scan_duration_seconds": result.get("scan_duration_seconds"),
        "analytics": result.get("analytics"),
    }


def watch_prop_arb(sport_key: str, event_id: str | None = None, bookmaker: str | None = None, region: str = "us", markets: str | None = None, provider: str = "odds-api", oddsblaze_sportsbooks: list[str] | None = None, oddsblaze_market_contains: str | None = None, interval: int = 30, bankroll: float | None = None, min_roi: float | None = None, min_profit: float | None = None, max_leg_age_seconds: float | None = None, max_cross_leg_update_gap_seconds: float | None = None, once: bool = False, as_json: bool = False, db_path: str = "data/opportunities.db", record_analytics: bool = False, inactive_after_seconds: int = 900) -> dict[str, Any]:
    seen: set[str] = set()
    iterations = 0
    alerts_sent = 0
    last_result: dict[str, Any] = {}
    while True:
        iterations += 1
        result = scan_prop_arb(sport_key, event_id=event_id, bookmaker=bookmaker, region=region, markets=markets, provider=provider, oddsblaze_sportsbooks=oddsblaze_sportsbooks, oddsblaze_market_contains=oddsblaze_market_contains, bankroll=bankroll, min_guaranteed_roi=min_roi, min_profit=min_profit, max_leg_age_seconds=max_leg_age_seconds, max_cross_leg_update_gap_seconds=max_cross_leg_update_gap_seconds, as_json=False, db_path=db_path, record_analytics=record_analytics, inactive_after_seconds=inactive_after_seconds)
        new_items = []
        for opp in result.get("prop_arbitrage_candidates", []):
            key = prop_opportunity_key(opp)
            if key in seen:
                continue
            seen.add(key)
            save_prop_opportunity(opp, db_path=db_path, sport=sport_key)
            alert = send_telegram_alert(opp, key, bankroll=bankroll)
            save_prop_alert(key, "telegram", alert.get("status", "unknown"), alert.get("error"), db_path=db_path)
            if alert.get("sent"):
                alerts_sent += 1
            new_items.append(opp)
        last_result = {"iterations": iterations, "new_opportunities": new_items, "alerts_sent": alerts_sent, "scan": result}
        if as_json:
            print(json.dumps(last_result, indent=2, sort_keys=True))
        else:
            for opp in new_items:
                print(f"NEW PROP ARB {opp.get('player')} {opp.get('prop_type')} {opp.get('line')} ROI={opp.get('guaranteed_roi')}")
        if once:
            return last_result
        time.sleep(interval)


def recent_prop_opportunities(limit: int = 20, db_path: str = "data/opportunities.db", as_json: bool = False) -> list[dict[str, Any]]:
    rows = load_prop_recent_opportunities(limit=limit, db_path=db_path)
    if as_json:
        print(json.dumps(rows, indent=2, sort_keys=True))
    else:
        for row in rows:
            print(f"{row.get('timestamp')} {row.get('player')} {row.get('market_type')} {row.get('line')} ROI={row.get('guaranteed_roi')}")
    return rows


def recent_prop_alerts(limit: int = 20, db_path: str = "data/opportunities.db", as_json: bool = False) -> list[dict[str, Any]]:
    rows = load_prop_recent_alerts(limit=limit, db_path=db_path)
    if as_json:
        print(json.dumps(rows, indent=2, sort_keys=True))
    else:
        for row in rows:
            print(f"{row.get('timestamp')} {row.get('destination')} {row.get('status')} {row.get('opportunity_key')}")
    return rows


def prop_stats(db_path: str = "data/opportunities.db", as_json: bool = False) -> dict[str, Any]:
    stats = prop_opportunity_stats(db_path=db_path)
    if as_json:
        print(json.dumps(stats, indent=2, sort_keys=True))
    else:
        print(f"Total opportunities: {stats['total_opportunities']}")
        print(f"Average ROI: {stats['average_roi']}")
        print(f"Best ROI: {stats['best_roi']}")
        print(f"Most common bookmaker pair: {stats['most_common_bookmaker_pair']}")
        print(f"Opportunities by sport: {stats['opportunities_by_sport']}")
    return stats


def opportunity_leaderboard(db_path: str = "data/opportunities.db", limit: int = 20, as_json: bool = False) -> dict[str, Any]:
    result = build_opportunity_leaderboard(db_path=db_path, limit=limit)
    if as_json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        for title, rows in result.items():
            print(title.replace("_", " ").title())
            for row in rows[:limit]:
                label = row.get(title[:-1]) or row.get(title.rstrip("s")) or row.get("sportsbook_pair") or row.get("provider") or row.get("prop_identity") or row.get("player")
                print(f"- {label}: appearances={row.get('appearances')} avg_roi={row.get('average_roi')} max_roi={row.get('max_roi')} median_roi={row.get('median_roi')} avg_life_s={row.get('average_lifetime_seconds')}")
    return result


def opportunity_lifetimes(db_path: str = "data/opportunities.db", limit: int = 20, as_json: bool = False) -> dict[str, Any]:
    result = build_opportunity_lifetimes(db_path=db_path, limit=limit)
    if as_json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print("Survival histogram")
        for bucket, count in result.get("survival_histogram", {}).items():
            print(f"- {bucket}: {count}")
        print(f"Average duration seconds: {result.get('average_duration_seconds')}")
        print("Longest-lived opportunities")
        for row in result.get("longest_lived", [])[:limit]:
            print(f"- {row.get('opportunity_id')} {row.get('player')} {row.get('prop_identity')} {row.get('sportsbook_pair')} duration={row.get('duration_seconds')}")
    return result


def opportunity_quality_report(db_path: str = "data/opportunities.db", as_json: bool = False) -> dict[str, Any]:
    result = build_opportunity_quality_report(db_path=db_path)
    if as_json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(f"Total rejections: {result.get('total_rejections')}")
        print(f"Stale leg rejection %: {result.get('stale_leg_rejection_percent')}")
        print(f"Period mismatch %: {result.get('period_mismatch_percent')}")
        print(f"Market mismatch %: {result.get('market_mismatch_percent')}")
        print(f"Invalid side %: {result.get('invalid_side_percent')}")
    return result


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


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value in {None, ""}:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


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


def telegram_test_alert(as_json: bool = False, db_path: str = "data/opportunities.db") -> dict[str, Any]:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        result = {"sent": False, "status": "skipped", "reason": "missing Telegram config"}
    else:
        result = send_telegram_alert(
            {
                "player": "Polylens Test",
                "prop_type": "telegram_test",
                "line": 0,
                "over_book": "test",
                "over_odds": 100,
                "under_book": "test",
                "under_odds": 100,
                "guaranteed_roi": 0,
                "guaranteed_profit_amount": 0,
            },
            "telegram_test",
        )
        result["reason"] = result.get("error") or result.get("status")
    record_alert_event(
        db_path=db_path,
        alert_type="telegram_test",
        channel="telegram",
        status=result.get("status", "unknown"),
        reason=result.get("reason"),
        raw={"TELEGRAM_BOT_TOKEN": token, "TELEGRAM_CHAT_ID": chat_id, "result": result},
    )
    if as_json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(f"Telegram test alert: {result.get('status')} ({result.get('reason')})")
    return result


def risk_status(db_path: str = "data/polylens.db", as_json: bool = False) -> dict[str, Any]:
    status = RiskEngine(db_path).status()
    if as_json:
        print(json.dumps(status, indent=2, sort_keys=True))
    else:
        print("Polylens Risk Status")
        print("=" * 20)
        print(f"Mode: {status['mode']}")
        print(f"DRY_RUN: {status['dry_run']}")
        print(f"LIVE_TRADING: {status['live_trading']}")
        print(f"Live execution enabled: {status['live_execution_enabled']}")
        print(f"Global halt: {status['global_halt']}")
        print(f"Active halts: {len(status['active_halts'])}")
        print(f"Exposure by venue: {status['exposure_by_venue']}")
    return status


def risk_events(limit: int = 20, db_path: str = "data/polylens.db", as_json: bool = False) -> list[dict[str, Any]]:
    rows = RiskEngine(db_path).recent_events(limit=limit)
    if as_json:
        print(json.dumps(rows, indent=2, sort_keys=True))
    else:
        for row in rows:
            print(f"{row.get('timestamp')} {row.get('decision')} {row.get('venue')} {row.get('market')} {row.get('reason')}")
    return rows


def risk_halt(reason: str = "manual halt", venue: str | None = None, db_path: str = "data/polylens.db", as_json: bool = False) -> dict[str, Any]:
    engine = RiskEngine(db_path)
    halt_id = engine.halt(reason=reason, venue=venue)
    result = {"id": halt_id, "active": True, "scope": "venue" if venue else "global", "venue": venue, "reason": reason}
    if as_json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(f"Risk halt active: {reason}")
    return result


def risk_resume(venue: str | None = None, db_path: str = "data/polylens.db", as_json: bool = False) -> dict[str, Any]:
    resumed = RiskEngine(db_path).resume(venue=venue)
    result = {"resumed_halts": resumed, "scope": "venue" if venue else "global", "venue": venue}
    if as_json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(f"Risk resume: resumed_halts={resumed}")
    return result


def _scan_short_crypto(assets: list[str], windows: list[int], as_json: bool = True) -> dict[str, Any]:
    from src.analysis.short_crypto_markets import normalize_short_crypto_markets
    from src.analysis.short_crypto_executor import ShortCryptoSignalEngine

    engine = ShortCryptoSignalEngine(assets=assets, windows=windows, min_edge=0.0)
    kalshi = _load_kalshi_short_crypto_markets(assets)
    polymarket = []
    try:
        polymarket = PolymarketClient(raw_dir="data/raw").get_active_markets(keyword="crypto", limit=200) or []
    except Exception as exc:  # pragma: no cover - CLI fallback path
        logging.getLogger(__name__).warning("polymarket market load failed for scan-short-crypto: %s", exc)
    normalized = normalize_short_crypto_markets(kalshi, polymarket)
    spot_map = _short_crypto_spot_map(assets)
    signals = engine.generate_signals(normalized, spot_map)
    rows = [
        {
            "asset": signal.asset,
            "direction": signal.direction,
            "venue": signal.venue,
            "ticker": signal.ticker,
            "window_minutes": signal.window_minutes,
            "spot_price": signal.spot_price,
            "edge": signal.edge,
            "roi": signal.roi,
            "timestamp": signal.timestamp,
        }
        for signal in signals
    ]
    payload = {"assets": assets, "windows": windows, "signal_count": len(rows), "signals": rows, "status": "ok"}
    if as_json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"Short crypto signals: {len(rows)}")
    return payload


def _watch_short_crypto(paper: bool = True, interval: int | None = None, max_loops: int | None = None, as_json: bool = True) -> dict[str, Any]:
    from src.analysis.short_crypto_markets import normalize_short_crypto_markets
    from src.analysis.short_crypto_executor import ShortCryptoSignalEngine, ShortCryptoExecutor, ShortCryptoRiskConfig

    assets = ["BTC", "ETH", "SOL"]
    engine = ShortCryptoSignalEngine(assets=assets, windows=[5, 10, 15], min_edge=0.0)
    executor = ShortCryptoExecutor(config=ShortCryptoRiskConfig.from_env())
    loop = 0
    executed_last: list[dict[str, Any]] = []
    while max_loops is None or loop < max_loops:
        kalshi = []
        polymarket = []
        kalshi = _load_kalshi_short_crypto_markets(assets)
        try:
            polymarket = PolymarketClient(raw_dir="data/raw").get_active_markets(keyword="crypto", limit=200) or []
        except Exception as exc:  # pragma: no cover - CLI fallback path
            logging.getLogger(__name__).warning("watch polymarket load failed: %s", exc)
        markets = normalize_short_crypto_markets(kalshi, polymarket)
        spot_map = _short_crypto_spot_map(assets)
        signals = engine.generate_signals(markets, spot_map)
        executed_last = []
        for signal in signals:
            result = executor.execute(signal, mode="paper" if paper else "live", live=not paper, max_loops=max_loops)
            executed_last.append({
                "asset": signal.asset,
                "direction": signal.direction,
                "venue": signal.venue,
                "ticker": signal.ticker,
                "accepted": result.get("accepted"),
                "reason": result.get("reason"),
                "stake": result.get("stake"),
            })
        payload = {"loop": loop, "paper": paper, "market_count": len(markets), "signal_count": len(signals), "executed": executed_last}
        if as_json:
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            print(f"Loop {loop}: signals={len(signals)} executed={len(executed_last)} paper={paper}")
        loop += 1
        if interval and (max_loops is None or loop < max_loops):
            time.sleep(int(interval))
    return {"loop": max(0, loop - 1), "paper": paper, "executed": executed_last}


def _trade_short_crypto(
    as_json: bool = True,
    paper: bool = True,
    live: bool = False,
    max_loops: int | None = None,
    venue: str = "kalshi",
    assets: list[str] | None = None,
    windows: list[int] | None = None,
    dry_run_live: bool = False,
) -> dict[str, Any]:
    from src.analysis.short_crypto_executor import ShortCryptoExecutor, ShortCryptoRiskConfig

    if dry_run_live and venue == "polymarket":
        from polymarket_live_order_audit import build_audit

        payload = build_audit(mode="short_crypto")
        result = {
            "accepted": False,
            "status": "dry_run_live",
            "mode": "live",
            "reason": "dry_run_live_no_order_sent" if payload.get("status") == "ready" else "polymarket_live_signing_not_ready",
            "audit": payload,
            "results": [payload],
        }
        if as_json:
            print(json.dumps(result, indent=2, sort_keys=True))
        else:
            print(result["reason"])
        return result

    assets = assets or ["BTC", "ETH", "SOL"]
    windows = windows or [5, 10, 15]
    executor = ShortCryptoExecutor(config=ShortCryptoRiskConfig.from_env())
    loops = max(1, int(max_loops or 1))
    results = []
    signals = _discover_short_crypto_signals(assets=assets, windows=windows, venue=venue)
    if (live or dry_run_live) and not signals:
        payload = {
            "accepted": False,
            "mode": "live" if live else "dry_run_live",
            "reason": "no_real_kalshi_short_crypto_market_discovered",
            "results": [],
        }
        if as_json:
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            print(payload["reason"])
        return payload
    if not signals and paper:
        signals = [_synthetic_short_crypto_signal(index=0)]
    for index in range(loops):
        signal = signals[index % len(signals)]
        if dry_run_live:
            selected = _signal_selected_yes_ask(signal)
            if not selected.get("ok"):
                results.append({"accepted": False, "status": "rejected", "mode": "live", "reason": "no_executable_resting_yes_ask", "ticker": signal.ticker})
                continue
            stake = executor._sized_stake(signal)
            price = selected["price_cents"] / 100.0
            if os.environ.get("POLYLENS_FIRST_LIVE_TEST", "").lower() in {"1", "true", "yes", "on"}:
                stake = min(stake, 1.0)
                count = 1
            else:
                count = min(max(1, int(stake / price)), int(selected["count"]))
            order_intent = executor._order_intent(signal, stake=stake, count=count, mode="live")
            order_intent["price"] = price
            order_intent["selected_liquidity_source"] = selected.get("derived_from")
            if selected.get("derived_from") == "no_bid":
                order_intent["action"] = "sell"
                order_intent["side"] = "no"
                order_intent.pop("yes_price_cents", None)
                order_intent["no_price_cents"] = int(selected["no_bid_cents"])
            else:
                order_intent["action"] = "buy"
                order_intent["side"] = "yes"
                order_intent["yes_price_cents"] = selected["price_cents"]
            order_intent["kalshi_payload"] = __import__("src.analysis.short_crypto_executor", fromlist=["build_kalshi_order_payload"]).build_kalshi_order_payload(order_intent)
            results.append({"accepted": False, "status": "dry_run_live", "mode": "live", "reason": "dry_run_live_no_order_sent", "selected_ask_price": price, "selected_ask_count": selected["count"], "selected_ask_raw": selected["raw"], "order": order_intent, "payload": order_intent.get("kalshi_payload")})
        else:
            results.append(executor.execute(signal, mode="live" if live and not paper else "paper", live=live and not paper, max_loops=loops))
    last = results[-1] if results else {}
    payload = {"accepted": last.get("accepted"), "mode": "live" if live and not paper else "paper", "reason": last.get("reason"), "stake": last.get("stake"), "results": results}
    if as_json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"Short crypto paper trade: accepted={payload['accepted']} reason={payload['reason']}")
    return payload


def _live_readiness_short_crypto(as_json: bool = True) -> dict[str, Any]:
    from src.analysis.short_crypto_executor import live_readiness_report

    payload = live_readiness_report()
    if as_json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"Short crypto live readiness: {payload['status']}")
    return payload


def _live_readiness_polymarket(as_json: bool = True) -> dict[str, Any]:
    from polymarket_live_order_audit import live_readiness_report

    payload = live_readiness_report()
    if as_json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"Polymarket live readiness: {payload['status']}")
    return payload



def _polymarket_auth_audit(as_json: bool = True) -> dict[str, Any]:
    from polymarket_auth_audit import build_auth_audit

    payload = build_auth_audit()
    if as_json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"Polymarket auth audit: {payload['status']}")
    return payload




def _polymarket_event_slug_audit(slug: str, as_json: bool = True) -> dict[str, Any]:
    from src.adapters.polymarket_live import audit_polymarket_event_slug

    payload = audit_polymarket_event_slug(slug)
    if as_json:
        print(json.dumps(payload, indent=2, sort_keys=True, default=str))
    else:
        print(f"Polymarket event slug audit ({slug}): {payload['diagnosis']}")
    return payload

def _polymarket_tradable_crypto_discovery(as_json: bool = True) -> dict[str, Any]:
    from src.adapters.polymarket_live import build_tradable_crypto_discovery

    payload = build_tradable_crypto_discovery()
    if as_json:
        print(json.dumps(payload, indent=2, sort_keys=True, default=str))
    else:
        summary = payload["summary"]
        print(f"Polymarket tradable crypto discovery: {summary['diagnosis']}")
    return payload

def _polymarket_credentials_setup(as_json: bool = True, write_env: bool = True) -> dict[str, Any]:
    from polymarket_credentials_setup import build_credentials_setup

    payload = build_credentials_setup(write_env_file=write_env)
    if as_json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"Polymarket credentials setup: {payload['status']}")
    return payload

def _short_crypto_spot_map(assets: list[str]) -> dict[str, float]:
    from src.adapters.crypto_price_feed import CryptoPriceFeedManager

    symbols = [f"{asset}-USD" for asset in assets]
    manager = CryptoPriceFeedManager(symbols=symbols)
    spot_map: dict[str, float] = {}
    try:
        manager.start()
        time.sleep(float(os.environ.get("POLYLENS_SHORT_CRYPTO_FEED_WARMUP_SECS", "0.25")))
        for asset, symbol in zip(assets, symbols):
            tick = manager.get_latest(symbol)
            if tick and (tick.mid or tick.last):
                spot_map[asset] = float(tick.mid or tick.last or 0.0)
    except Exception as exc:
        logging.getLogger(__name__).warning("short crypto price feed unavailable: %s", exc)
    finally:
        try:
            manager.stop()
        except Exception:
            pass
    if not spot_map:
        spot_map.update(_coinbase_rest_spot_map(assets))
    return spot_map


def _coinbase_rest_spot_map(assets: list[str]) -> dict[str, float]:
    from urllib.request import Request, urlopen

    prices: dict[str, float] = {}
    for asset in assets:
        symbol = f"{asset}-USD"
        try:
            request = Request(f"https://api.exchange.coinbase.com/products/{symbol}/ticker", headers={"User-Agent": "polylens/0.1", "Accept": "application/json"})
            with urlopen(request, timeout=5) as response:
                payload = json.loads(response.read().decode("utf-8"))
            price = float(payload.get("price") or 0.0)
        except Exception as exc:
            logging.getLogger(__name__).warning("coinbase rest price unavailable symbol=%s: %s", symbol, exc)
            price = 0.0
        if price > 0:
            prices[asset] = price
    return prices


def _load_kalshi_short_crypto_markets(assets: list[str]) -> list[dict[str, Any]]:
    series_by_asset = {"BTC": ["KXBTCD", "KXBTC"], "ETH": ["KXETHD", "KXETH"], "SOL": ["KXSOLD"]}
    client = KalshiClient(raw_dir="data/raw")
    markets: list[dict[str, Any]] = []
    for asset in assets:
        for series in series_by_asset.get(asset, []):
            try:
                markets.extend(client.get_markets(status="open", limit=100, max_pages=1, series_ticker=series) or [])
            except Exception as exc:
                logging.getLogger(__name__).warning("kalshi short crypto series load failed series=%s: %s", series, exc)
    return markets


def _discover_short_crypto_signals(assets: list[str], windows: list[int], venue: str = "kalshi") -> list[Any]:
    from src.analysis.short_crypto_executor import ShortCryptoSignalEngine
    from src.analysis.short_crypto_executor import select_executable_yes_ask
    from src.analysis.short_crypto_markets import normalize_short_crypto_markets

    if venue != "kalshi":
        return []
    markets = normalize_short_crypto_markets(_load_kalshi_short_crypto_markets(assets), [])
    fresh_markets = []
    client = KalshiClient(raw_dir="data/raw")
    now = time.time()
    for market in markets:
        try:
            book = client.get_orderbook(market.ticker)
        except Exception as exc:
            logging.getLogger(__name__).warning("kalshi short crypto orderbook load failed ticker=%s: %s", market.ticker, exc)
            continue
        if not _kalshi_orderbook_has_depth(book):
            continue
        selected = select_executable_yes_ask(book)
        if not selected.get("ok"):
            continue
        fresh_markets.append(type(market)(
            asset=market.asset,
            venue=market.venue,
            ticker=market.ticker,
            start_ts=market.start_ts,
            end_ts=market.end_ts,
            direction=market.direction,
            yes_bid=market.yes_bid,
            yes_ask=market.yes_ask,
            no_bid=market.no_bid,
            no_ask=market.no_ask,
            liquidity=market.liquidity or 1.0,
            window_minutes=None,
            strike_price=market.strike_price,
            reference_price=market.reference_price,
            timestamp=now,
            raw={**(market.raw or {}), "orderbook": book, "selected_yes_ask": selected},
        ))
        if len(fresh_markets) >= max(1, len(assets)):
            break
    spot_map = _short_crypto_spot_map(assets)
    for market in fresh_markets:
        if market.asset not in spot_map and market.strike_price:
            spot_map[market.asset] = float(market.strike_price)
    refreshed = [
        type(market)(
            asset=market.asset,
            venue=market.venue,
            ticker=market.ticker,
            start_ts=market.start_ts,
            end_ts=market.end_ts,
            direction=market.direction,
            yes_bid=market.yes_bid,
            yes_ask=market.yes_ask,
            no_bid=market.no_bid,
            no_ask=market.no_ask,
            liquidity=market.liquidity,
            window_minutes=market.window_minutes,
            strike_price=market.strike_price,
            reference_price=market.reference_price,
            timestamp=time.time(),
            raw=market.raw,
        )
        for market in fresh_markets
    ]
    return ShortCryptoSignalEngine(assets=assets, windows=windows, min_edge=0.0).generate_signals(refreshed, spot_map)


def _kalshi_orderbook_has_depth(book: dict[str, Any]) -> bool:
    orderbook = (book or {}).get("orderbook") or (book or {}).get("orderbook_fp") or book or {}
    for key in ("yes", "no", "yes_dollars", "no_dollars"):
        levels = orderbook.get(key) if isinstance(orderbook, dict) else None
        if isinstance(levels, list) and levels:
            return True
    return False


def _signal_selected_yes_ask(signal: Any) -> dict[str, Any]:
    market = signal.meta.get("market") if isinstance(signal.meta, dict) else None
    raw = getattr(market, "raw", None) or {}
    selected = raw.get("selected_yes_ask")
    return selected if isinstance(selected, dict) else {"ok": False, "reason": "no_executable_resting_yes_ask"}


def _synthetic_short_crypto_signal(index: int = 0) -> Any:
    from src.analysis.short_crypto_executor import CryptoSignal
    from src.analysis.short_crypto_markets import ShortCryptoMarket

    now = time.time()
    market = ShortCryptoMarket(
        asset="BTC",
        venue="kalshi",
        ticker=f"BTC-UP-5-PAPER-{index}-{int(now)}",
        start_ts=now,
        end_ts=now + 300,
        direction="up",
        yes_bid=0.5,
        yes_ask=0.55,
        no_bid=0.45,
        no_ask=0.5,
        liquidity=100.0,
        window_minutes=5,
        timestamp=now,
    )
    return CryptoSignal(
        asset="BTC",
        window_minutes=5,
        direction="up",
        venue="kalshi",
        ticker=market.ticker,
        spot_price=100.0,
        implied_prob=0.525,
        model_prob=0.6,
        edge=0.075,
        roi=0.1,
        timestamp=now,
        meta={"market": market, "price_timestamp": now},
    )


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

    wallet_forensics_parser = sub.add_parser("wallet-forensics", help="offline wallet behavior forensics from exported activity JSON")
    wallet_forensics_parser.add_argument("--wallet", help="wallet address (optional if present in input JSON)")
    wallet_forensics_parser.add_argument("--input-json", required=True, help="path to exported Polymarket activity JSON")
    wallet_forensics_parser.add_argument("--json", action="store_true", help="emit forensics report as JSON")


    wallet_activity_parser = sub.add_parser("export-wallet-activity", help="export normalized Polymarket wallet activity")
    wallet_activity_parser.add_argument("--wallet", required=True)
    wallet_activity_parser.add_argument("--output")
    wallet_activity_parser.add_argument("--limit", type=int)
    wallet_activity_parser.add_argument("--db-path", default=DEFAULT_WALLET_ACTIVITY_DB)
    wallet_activity_parser.add_argument("--json", action="store_true")

    analyze_trader_parser = sub.add_parser("analyze-trader", help="export wallet activity, run wallet forensics, and update trader registry")
    analyze_trader_parser.add_argument("--wallet", required=True)
    analyze_trader_parser.add_argument("--limit", type=int)
    analyze_trader_parser.add_argument("--output")
    analyze_trader_parser.add_argument("--db-path", default=DEFAULT_WALLET_ACTIVITY_DB)
    analyze_trader_parser.add_argument("--traders-db-path", default="data/traders.db")
    analyze_trader_parser.add_argument("--json", action="store_true")

    scan_top_traders_parser = sub.add_parser("scan-top-traders", help="discover, analyze, classify, and rank trader wallets")
    scan_top_traders_parser.add_argument("--wallet")
    scan_top_traders_parser.add_argument("--watchlist", default=DEFAULT_WATCHLIST_PATH)
    scan_top_traders_parser.add_argument("--limit", type=int)
    scan_top_traders_parser.add_argument("--json", action="store_true")

    discover_traders_parser = sub.add_parser("discover-traders", help="discover candidate trader wallets for intelligence scanning")
    discover_traders_parser.add_argument("--wallet")
    discover_traders_parser.add_argument("--activity-export")
    discover_traders_parser.add_argument("--watchlist", default=DEFAULT_WATCHLIST_PATH)
    discover_traders_parser.add_argument("--limit", type=int)
    discover_traders_parser.add_argument("--scan", action="store_true")
    discover_traders_parser.add_argument("--json", action="store_true")

    trader_registry_summary_parser = sub.add_parser("trader-registry-summary", help="summary of tracked trader wallets")
    trader_registry_summary_parser.add_argument("--classification", choices=["market_maker", "arbitrage_trader", "quantitative_directional", "mixed", "unknown"])
    trader_registry_summary_parser.add_argument("--min-watch-score", type=int, default=0)
    trader_registry_summary_parser.add_argument("--limit", type=int, default=100)
    trader_registry_summary_parser.add_argument("--db-path", default="data/traders.db")
    trader_registry_summary_parser.add_argument("--json", action="store_true")

    trader_leaderboard_parser = sub.add_parser("trader-leaderboard", help="top traders ranked by watch score")
    trader_leaderboard_parser.add_argument("--limit", type=int, default=20)
    trader_leaderboard_parser.add_argument("--classification", choices=["market_maker", "arbitrage_trader", "quantitative_directional", "mixed", "unknown"])
    trader_leaderboard_parser.add_argument("--db-path", default="data/traders.db")
    trader_leaderboard_parser.add_argument("--json", action="store_true")

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

    kalshi_markets_parser = sub.add_parser("kalshi-markets")
    kalshi_markets_parser.add_argument("--limit", type=int, default=20)
    kalshi_markets_parser.add_argument("--json", action="store_true")

    kalshi_orderbook_parser = sub.add_parser("kalshi-orderbook")
    kalshi_orderbook_parser.add_argument("--ticker", required=True)
    kalshi_orderbook_parser.add_argument("--json", action="store_true")

    kalshi_scan_parser = sub.add_parser("kalshi-paper-scan")
    kalshi_scan_parser.add_argument("--limit", type=int, default=20)
    kalshi_scan_parser.add_argument("--max-price", type=float, default=0.5)
    kalshi_scan_parser.add_argument("--json", action="store_true")

    kalshi_trade_parser = sub.add_parser("kalshi-paper-trade")
    kalshi_trade_parser.add_argument("--ticker", required=True)
    kalshi_trade_parser.add_argument("--side", required=True, choices=["yes", "no"])
    kalshi_trade_parser.add_argument("--price", required=True, type=float)
    kalshi_trade_parser.add_argument("--count", required=True, type=int)
    kalshi_trade_parser.add_argument("--json", action="store_true")

    kalshi_status_parser = sub.add_parser("kalshi-status")
    kalshi_status_parser.add_argument("--json", action="store_true")

    kalshi_live_smoke_parser = sub.add_parser("kalshi-live-smoke-test")
    kalshi_live_smoke_parser.add_argument("--ticker", required=True)
    kalshi_live_smoke_parser.add_argument("--side", required=True, choices=["yes", "no"])
    kalshi_live_smoke_parser.add_argument("--price", required=True, type=float)
    kalshi_live_smoke_parser.add_argument("--count", required=True, type=int)
    kalshi_live_smoke_parser.add_argument("--max-notional", type=float, default=1.0)
    kalshi_live_smoke_parser.add_argument("--json", action="store_true")

    kalshi_account_parser = sub.add_parser("kalshi-account")
    kalshi_account_parser.add_argument("--json", action="store_true")

    kalshi_balance_parser = sub.add_parser("kalshi-balance")
    kalshi_balance_parser.add_argument("--json", action="store_true")

    kalshi_positions_parser = sub.add_parser("kalshi-positions")
    kalshi_positions_parser.add_argument("--limit", type=int, default=100)
    kalshi_positions_parser.add_argument("--json", action="store_true")

    kalshi_orders_parser = sub.add_parser("kalshi-orders")
    kalshi_orders_parser.add_argument("--limit", type=int, default=100)
    kalshi_orders_parser.add_argument("--json", action="store_true")

    kalshi_report_parser = sub.add_parser("kalshi-report")
    kalshi_report_parser.add_argument("--json", action="store_true")

    kalshi_export_parser = sub.add_parser("kalshi-export")
    kalshi_export_parser.add_argument("--json", action="store_true")

    kalshi_export_history_parser = sub.add_parser("kalshi-export-account-history")
    kalshi_export_history_parser.add_argument("--output", default=DEFAULT_ACCOUNT_HISTORY_PATH)
    kalshi_export_history_parser.add_argument("--json", action="store_true")

    kalshi_patterns_parser = sub.add_parser("kalshi-patterns")
    kalshi_patterns_parser.add_argument("--json", action="store_true")

    kalshi_sim_parser = sub.add_parser("kalshi-simulate")
    kalshi_sim_parser.add_argument("--assets")
    kalshi_sim_parser.add_argument("--market-types")
    kalshi_sim_parser.add_argument("--price-bands")
    kalshi_sim_parser.add_argument("--max-contracts", type=int, default=1)
    kalshi_sim_parser.add_argument("--bankroll", type=float, default=1000.0)
    kalshi_sim_parser.add_argument("--fee-assumption", type=float, default=0.0)
    kalshi_sim_parser.add_argument("--strategy-mode", choices=["extreme-probability", "mean-reversion", "momentum", "no-trade-baseline"], default="extreme-probability")
    kalshi_sim_parser.add_argument("--export", action="store_true")
    kalshi_sim_parser.add_argument("--json", action="store_true")

    kalshi_backtest_parser = sub.add_parser("kalshi-backtest")
    kalshi_backtest_parser.add_argument("--db-path", default=DEFAULT_KALSHI_DATA_DB)
    kalshi_backtest_parser.add_argument("--strategy", choices=["all", "spread-compression", "momentum", "mean-reversion", "probability-extremes"], default="all")
    kalshi_backtest_parser.add_argument("--fee-assumption", type=float, default=0.0)
    kalshi_backtest_parser.add_argument("--spread-threshold", type=float, default=0.05)
    kalshi_backtest_parser.add_argument("--bankroll", type=float, default=1000.0)
    kalshi_backtest_parser.add_argument("--export", action="store_true")
    kalshi_backtest_parser.add_argument("--json", action="store_true")

    kalshi_backtest_summary_parser = sub.add_parser("kalshi-backtest-summary")
    kalshi_backtest_summary_parser.add_argument("--db-path", default=DEFAULT_KALSHI_DATA_DB)
    kalshi_backtest_summary_parser.add_argument("--json", action="store_true")

    kalshi_record_parser = sub.add_parser("kalshi-record-markets")
    kalshi_record_parser.add_argument("--assets")
    kalshi_record_parser.add_argument("--market-types")
    kalshi_record_parser.add_argument("--interval", type=int, default=60)
    kalshi_record_parser.add_argument("--duration-minutes", type=float)
    kalshi_record_parser.add_argument("--limit", type=int, default=100, help="save up to this many matching markets per poll")
    kalshi_record_parser.add_argument("--discovery-limit", type=int, default=1000, help="inspect up to this many raw markets before local filtering")
    kalshi_record_parser.add_argument("--event-ticker-prefix")
    kalshi_record_parser.add_argument("--ticker-prefix")
    kalshi_record_parser.add_argument("--db-path", default=DEFAULT_KALSHI_DATA_DB)
    kalshi_record_parser.add_argument("--json", action="store_true")

    kalshi_data_summary_parser = sub.add_parser("kalshi-data-summary")
    kalshi_data_summary_parser.add_argument("--db-path", default=DEFAULT_KALSHI_DATA_DB)
    kalshi_data_summary_parser.add_argument("--json", action="store_true")

    opportunity_ranker_parser = sub.add_parser("opportunity-ranker")
    opportunity_ranker_parser.add_argument("--venue", choices=["kalshi", "polymarket", "sportsbook"])
    opportunity_ranker_parser.add_argument("--market-type", choices=["crypto", "sports", "event", "prop", "futures"])
    opportunity_ranker_parser.add_argument("--asset", choices=["BTC", "ETH", "SOL"])
    opportunity_ranker_parser.add_argument("--sport", choices=["MLB", "NBA", "NFL", "NHL"])
    opportunity_ranker_parser.add_argument("--min-roi", type=float)
    opportunity_ranker_parser.add_argument("--min-confidence", type=float)
    opportunity_ranker_parser.add_argument("--max-age-seconds", type=int)
    opportunity_ranker_parser.add_argument("--limit", type=int, default=20)
    opportunity_ranker_parser.add_argument("--json", action="store_true")
    opportunity_ranker_parser.add_argument("--export", action="store_true")

    sports_parser = sub.add_parser("list-sportsbooks")
    sports_parser.add_argument("--json", action="store_true", help="emit sports list as JSON")

    odds_parser = sub.add_parser("fetch-odds")
    odds_parser.add_argument("--sport", required=True, dest="sport_key")
    odds_parser.add_argument("--bookmaker")
    odds_parser.add_argument("--region", default="us")
    odds_parser.add_argument("--markets", default="h2h,spreads,totals")
    odds_parser.add_argument("--json", action="store_true")

    props_parser = sub.add_parser("fetch-player-props")
    props_parser.add_argument("--sport", required=True, dest="sport_key")
    props_parser.add_argument("--event-id")
    props_parser.add_argument("--bookmaker")
    props_parser.add_argument("--region", default="us")
    props_parser.add_argument("--markets")
    props_parser.add_argument("--json", action="store_true")

    oddsblaze_parser = sub.add_parser("oddsblaze-odds")
    oddsblaze_parser.add_argument("--sportsbook", required=True)
    oddsblaze_parser.add_argument("--league", required=True)
    oddsblaze_parser.add_argument("--market")
    oddsblaze_parser.add_argument("--market-contains")
    oddsblaze_parser.add_argument("--main")
    oddsblaze_parser.add_argument("--live")
    oddsblaze_parser.add_argument("--json", action="store_true")

    debug_props_parser = sub.add_parser("debug-player-props")
    debug_props_parser.add_argument("--sport", required=True, dest="sport_key")
    debug_props_parser.add_argument("--event-id")
    debug_props_parser.add_argument("--bookmaker")
    debug_props_parser.add_argument("--region", default="us")
    debug_props_parser.add_argument("--markets")
    debug_props_parser.add_argument("--json", action="store_true")

    prop_arb_parser = sub.add_parser("scan-prop-arb")
    prop_arb_parser.add_argument("--sport", required=True, dest="sport_key")
    prop_arb_parser.add_argument("--event-id")
    prop_arb_parser.add_argument("--bookmaker")
    prop_arb_parser.add_argument("--sportsbook", action="append", dest="oddsblaze_sportsbooks", help="OddsBlaze sportsbook; repeat or pass comma-separated values")
    prop_arb_parser.add_argument("--oddsblaze-market-contains", help="OddsBlaze market_contains override for provider oddsblaze/all")
    prop_arb_parser.add_argument("--provider", choices=["odds-api", "oddsblaze", "all"], default="odds-api")
    prop_arb_parser.add_argument("--region", default="us")
    prop_arb_parser.add_argument("--markets")
    prop_arb_parser.add_argument("--bankroll", type=float)
    prop_arb_parser.add_argument("--min-guaranteed-roi", type=float)
    prop_arb_parser.add_argument("--min-roi", type=float, dest="min_roi")
    prop_arb_parser.add_argument("--min-profit", type=float)
    prop_arb_parser.add_argument("--max-leg-age-seconds", type=float, default=180)
    prop_arb_parser.add_argument("--max-cross-leg-update-gap-seconds", type=float, default=300)
    prop_arb_parser.add_argument("--profile")
    prop_arb_parser.add_argument("--sportsbooks")
    prop_arb_parser.add_argument("--db-path", default="data/opportunities.db")
    prop_arb_parser.add_argument("--json", action="store_true")
    prop_arb_parser.add_argument("--summary-json", action="store_true", help="emit compact scan summary JSON without full candidate payloads")

    watch_prop_parser = sub.add_parser("watch-prop-arb")
    watch_prop_parser.add_argument("--sport", required=True, dest="sport_key")
    watch_prop_parser.add_argument("--event-id")
    watch_prop_parser.add_argument("--bookmaker")
    watch_prop_parser.add_argument("--sportsbook", action="append", dest="oddsblaze_sportsbooks", help="OddsBlaze sportsbook; repeat or pass comma-separated values")
    watch_prop_parser.add_argument("--oddsblaze-market-contains", help="OddsBlaze market_contains override for provider oddsblaze/all")
    watch_prop_parser.add_argument("--provider", choices=["odds-api", "oddsblaze", "all"], default="odds-api")
    watch_prop_parser.add_argument("--region", default="us")
    watch_prop_parser.add_argument("--markets")
    watch_prop_parser.add_argument("--interval", type=int, default=30)
    watch_prop_parser.add_argument("--bankroll", type=float)
    watch_prop_parser.add_argument("--min-roi", type=float)
    watch_prop_parser.add_argument("--min-profit", type=float)
    watch_prop_parser.add_argument("--max-leg-age-seconds", type=float, default=180)
    watch_prop_parser.add_argument("--max-cross-leg-update-gap-seconds", type=float, default=300)
    watch_prop_parser.add_argument("--once", action="store_true")
    watch_prop_parser.add_argument("--json", action="store_true")
    watch_prop_parser.add_argument("--db-path", default="data/opportunities.db")

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

    opportunity_leaderboard_parser = sub.add_parser("opportunity-leaderboard")
    opportunity_leaderboard_parser.add_argument("--db-path", default="data/opportunities.db")
    opportunity_leaderboard_parser.add_argument("--limit", type=int, default=20)
    opportunity_leaderboard_parser.add_argument("--json", action="store_true")

    opportunity_lifetimes_parser = sub.add_parser("opportunity-lifetimes")
    opportunity_lifetimes_parser.add_argument("--db-path", default="data/opportunities.db")
    opportunity_lifetimes_parser.add_argument("--limit", type=int, default=20)
    opportunity_lifetimes_parser.add_argument("--json", action="store_true")

    opportunity_quality_parser = sub.add_parser("opportunity-quality-report")
    opportunity_quality_parser.add_argument("--db-path", default="data/opportunities.db")
    opportunity_quality_parser.add_argument("--json", action="store_true")

    telegram_test_parser = sub.add_parser("telegram-test-alert")
    telegram_test_parser.add_argument("--db-path", default="data/opportunities.db")
    telegram_test_parser.add_argument("--json", action="store_true")

    risk_status_parser = sub.add_parser("risk-status")
    risk_status_parser.add_argument("--db-path", default="data/polylens.db")
    risk_status_parser.add_argument("--json", action="store_true")

    risk_events_parser = sub.add_parser("risk-events")
    risk_events_parser.add_argument("--limit", type=int, default=20)
    risk_events_parser.add_argument("--db-path", default="data/polylens.db")
    risk_events_parser.add_argument("--json", action="store_true")

    risk_halt_parser = sub.add_parser("risk-halt")
    risk_halt_parser.add_argument("--reason", default="manual halt")
    risk_halt_parser.add_argument("--venue")
    risk_halt_parser.add_argument("--db-path", default="data/polylens.db")
    risk_halt_parser.add_argument("--json", action="store_true")

    risk_resume_parser = sub.add_parser("risk-resume")
    risk_resume_parser.add_argument("--venue")
    risk_resume_parser.add_argument("--db-path", default="data/polylens.db")
    risk_resume_parser.add_argument("--json", action="store_true")

    scan_short_crypto_parser = sub.add_parser("scan-short-crypto")
    scan_short_crypto_parser.add_argument("--assets", required=True, help="comma-separated assets, e.g. BTC,ETH,SOL")
    scan_short_crypto_parser.add_argument("--windows", required=True, help="comma-separated window minutes, e.g. 5,10,15")
    scan_short_crypto_parser.add_argument("--json", action="store_true")

    watch_short_crypto_parser = sub.add_parser("watch-short-crypto")
    watch_short_crypto_parser.add_argument("--paper", action="store_true", default=True, help="force paper execution (default)")
    watch_short_crypto_parser.add_argument("--interval", type=int, default=None, help="seconds between loops")
    watch_short_crypto_parser.add_argument("--max-loops", type=int, default=None, help="stop after N loops")
    watch_short_crypto_parser.add_argument("--json", action="store_true")

    trade_short_crypto_parser = sub.add_parser("trade-short-crypto")
    trade_short_crypto_parser.add_argument("--paper", action="store_true", default=True, help="paper trading only")
    trade_short_crypto_parser.add_argument("--live", action="store_true", default=False, help="request live execution; still requires env readiness gates")
    trade_short_crypto_parser.add_argument("--dry-run-live", action="store_true", default=False, help="build live Kalshi payload for a real market without sending")
    trade_short_crypto_parser.add_argument("--venue", default="kalshi", choices=["kalshi", "polymarket"])
    trade_short_crypto_parser.add_argument("--assets", default="BTC,ETH,SOL", help="comma-separated assets, e.g. BTC,ETH,SOL")
    trade_short_crypto_parser.add_argument("--windows", default="5,10,15", help="comma-separated window minutes, e.g. 5,10,15")
    trade_short_crypto_parser.add_argument("--max-loops", type=int, default=None, help="stop after N trade loops")
    trade_short_crypto_parser.add_argument("--json", action="store_true")

    short_crypto_paper_run_parser = sub.add_parser("short-crypto-paper-run")
    short_crypto_paper_run_parser.add_argument("--venues", default="kalshi,polymarket")
    short_crypto_paper_run_parser.add_argument("--assets", default="BTC,ETH,SOL")
    short_crypto_paper_run_parser.add_argument("--windows", default="5,10,15")
    short_crypto_paper_run_parser.add_argument("--max-trades", type=int, default=10)
    short_crypto_paper_run_parser.add_argument("--max-paper-exposure", type=float, default=100.0)
    short_crypto_paper_run_parser.add_argument("--min-edge", type=float, default=0.01)
    short_crypto_paper_run_parser.add_argument("--min-liquidity", type=float, default=1.0)
    short_crypto_paper_run_parser.add_argument("--freshness-seconds", type=float, default=30.0)
    short_crypto_paper_run_parser.add_argument("--max-market-lead-time-minutes", type=float)
    short_crypto_paper_run_parser.add_argument("--db-path", default="data/short_crypto_paper.db")
    short_crypto_paper_run_parser.add_argument("--discover-only", action="store_true")
    short_crypto_paper_run_parser.add_argument("--directions", help="Comma-separated paper-only direction filter, e.g. up,down")
    short_crypto_paper_run_parser.add_argument("--max-model-probability", type=float)
    short_crypto_paper_run_parser.add_argument("--max-entry-price", type=float)
    short_crypto_paper_run_parser.add_argument("--min-entry-price", type=float)
    short_crypto_paper_run_parser.add_argument("--require-volatility-above-median", action="store_true")
    short_crypto_paper_run_parser.add_argument("--strategy-label")
    short_crypto_paper_run_parser.add_argument("--json", action="store_true")

    short_crypto_paper_settle_parser = sub.add_parser("short-crypto-paper-settle")
    short_crypto_paper_settle_parser.add_argument("--db-path", default="data/short_crypto_paper.db")
    short_crypto_paper_settle_parser.add_argument("--json", action="store_true")

    short_crypto_paper_report_parser = sub.add_parser("short-crypto-paper-report")
    short_crypto_paper_report_parser.add_argument("--db-path", default="data/short_crypto_paper.db")
    short_crypto_paper_report_parser.add_argument("--json", action="store_true")
    short_crypto_paper_report_parser.add_argument("--verbose", action="store_true")

    strategy_feedback_parser = sub.add_parser("strategy-feedback")
    strategy_feedback_parser.add_argument("--db-path", default="data/short_crypto_paper.db")
    strategy_feedback_parser.add_argument("--min-trades", type=int, default=25)
    strategy_feedback_parser.add_argument("--max-adjustment", type=float, default=0.10)
    strategy_feedback_parser.add_argument("--json", action="store_true")

    strategy_recommendations_parser = sub.add_parser("strategy-recommendations")
    strategy_recommendations_parser.add_argument("--db-path", default="data/short_crypto_paper.db")
    strategy_recommendations_parser.add_argument("--min-trades", type=int, default=25)
    strategy_recommendations_parser.add_argument("--max-adjustment", type=float, default=0.10)
    strategy_recommendations_parser.add_argument("--json", action="store_true")

    short_crypto_feedback_loop_parser = sub.add_parser("short-crypto-feedback-loop")
    short_crypto_feedback_loop_parser.add_argument("--db-path", default="data/short_crypto_paper.db")
    short_crypto_feedback_loop_parser.add_argument("--min-trades", type=int, default=25)
    short_crypto_feedback_loop_parser.add_argument("--max-adjustment", type=float, default=0.10)
    short_crypto_feedback_loop_parser.add_argument("--json", action="store_true")

    short_crypto_paper_diagnostics_parser = sub.add_parser("short-crypto-paper-diagnostics")
    short_crypto_paper_diagnostics_parser.add_argument("--db-path", default="data/short_crypto_paper.db")
    short_crypto_paper_diagnostics_parser.add_argument("--json", action="store_true")
    short_crypto_paper_diagnostics_parser.add_argument("--no-refresh", action="store_true")

    short_crypto_paper_calibration_parser = sub.add_parser("short-crypto-paper-calibration")
    short_crypto_paper_calibration_parser.add_argument("--db-path", default="data/short_crypto_paper.db")
    short_crypto_paper_calibration_parser.add_argument("--json", action="store_true")
    short_crypto_paper_calibration_parser.add_argument("--no-refresh", action="store_true")
    short_crypto_paper_calibration_parser.add_argument("--min-segment-trades", type=int, default=20)

    short_crypto_paper_settlement_audit_parser = sub.add_parser("short-crypto-paper-settlement-audit")
    short_crypto_paper_settlement_audit_parser.add_argument("--db-path", default="data/short_crypto_paper.db")
    short_crypto_paper_settlement_audit_parser.add_argument("--json", action="store_true")

    live_ready_short_crypto_parser = sub.add_parser("live-readiness-short-crypto")
    live_ready_short_crypto_parser.add_argument("--json", action="store_true")

    live_ready_polymarket_parser = sub.add_parser("live-readiness-polymarket")
    live_ready_polymarket_parser.add_argument("--json", action="store_true")

    polymarket_auth_audit_parser = sub.add_parser("polymarket-auth-audit")
    polymarket_auth_audit_parser.add_argument("--json", action="store_true")

    polymarket_credentials_setup_parser = sub.add_parser("polymarket-credentials-setup")
    polymarket_credentials_setup_parser.add_argument("--json", action="store_true")
    polymarket_credentials_setup_parser.add_argument("--no-write-env", action="store_true")

    polymarket_tradable_crypto_discovery_parser = sub.add_parser("polymarket-tradable-crypto-discovery")
    polymarket_tradable_crypto_discovery_parser.add_argument("--json", action="store_true")

    polymarket_event_slug_audit_parser = sub.add_parser("polymarket-event-slug-audit")
    polymarket_event_slug_audit_parser.add_argument("slug")
    polymarket_event_slug_audit_parser.add_argument("--json", action="store_true")

    web_dashboard_parser = sub.add_parser("web-dashboard", help="run the Polylens NiceGUI web dashboard")
    web_dashboard_parser.add_argument("--host", default="127.0.0.1")
    web_dashboard_parser.add_argument("--port", type=int, default=8787)

    args = parser.parse_args()

    if args.command == "analyze-wallet":
        analyze_wallet(args.wallet)
    elif args.command == "export-wallet":
        export_wallet(args.wallet, include_kalshi=args.include_kalshi or args.include_pricing, include_pricing=args.include_pricing)
    elif args.command == "wallet-forensics":
        wallet_forensics_cli(wallet=args.wallet, input_json=args.input_json, as_json=args.json)
    elif args.command == "export-wallet-activity":
        export_wallet_activity_cli(wallet=args.wallet, output=args.output, limit=args.limit, as_json=args.json, db_path=args.db_path)
    elif args.command == "analyze-trader":
        analyze_trader_cli(wallet=args.wallet, limit=args.limit, as_json=args.json, output=args.output, db_path=args.db_path, traders_db_path=args.traders_db_path)
    elif args.command == "scan-top-traders":
        scan_top_traders_cli(wallet=args.wallet, watchlist=args.watchlist, limit=args.limit, as_json=args.json)
    elif args.command == "discover-traders":
        discover_traders_cli(wallet=args.wallet, activity_export=args.activity_export, watchlist=args.watchlist, limit=args.limit, scan=args.scan, as_json=args.json)
    elif args.command == "trader-registry-summary":
        trader_registry_summary_cli(
            classification=args.classification,
            min_watch_score=args.min_watch_score,
            limit=args.limit,
            as_json=args.json,
            db_path=args.db_path,
        )
    elif args.command == "trader-leaderboard":
        trader_leaderboard_cli(
            limit=args.limit,
            classification=args.classification,
            as_json=args.json,
            db_path=args.db_path,
        )
    elif args.command == "compare-kalshi":
        compare_kalshi(args.wallet)
    elif args.command == "scan-arb":
        scan_arb(args.wallet)
    elif args.command == "explain-matches":
        explain_matches(args.wallet, as_json=args.json, save=args.save, db_path=args.db_path)
    elif args.command == "market-inventory":
        market_inventory(args.wallet, include_closed=args.include_closed, as_json=args.json)
    elif args.command == "kalshi-markets":
        kalshi_markets_cli(limit=args.limit, as_json=args.json)
    elif args.command == "kalshi-orderbook":
        kalshi_orderbook_cli(args.ticker, as_json=args.json)
    elif args.command == "kalshi-paper-scan":
        kalshi_paper_scan(limit=args.limit, max_price=args.max_price, as_json=args.json)
    elif args.command == "kalshi-paper-trade":
        kalshi_paper_trade(args.ticker, args.side, args.price, args.count, as_json=args.json)
    elif args.command == "kalshi-status":
        kalshi_status(as_json=args.json)
    elif args.command == "kalshi-live-smoke-test":
        kalshi_live_smoke_test(args.ticker, args.side, args.price, args.count, max_notional=args.max_notional, as_json=args.json)
    elif args.command == "kalshi-account":
        kalshi_account(as_json=args.json)
    elif args.command == "kalshi-balance":
        kalshi_balance(as_json=args.json)
    elif args.command == "kalshi-positions":
        kalshi_positions(limit=args.limit, as_json=args.json)
    elif args.command == "kalshi-orders":
        kalshi_orders(limit=args.limit, as_json=args.json)
    elif args.command == "kalshi-report":
        kalshi_report(as_json=args.json)
    elif args.command == "kalshi-export":
        kalshi_export(as_json=args.json)
    elif args.command == "kalshi-export-account-history":
        kalshi_export_account_history(output=args.output, as_json=args.json)
    elif args.command == "kalshi-patterns":
        kalshi_patterns(as_json=args.json)
    elif args.command == "kalshi-simulate":
        kalshi_simulate(assets=args.assets, market_types=args.market_types, price_bands=args.price_bands, max_contracts=args.max_contracts, bankroll=args.bankroll, fee_assumption=args.fee_assumption, strategy_mode=args.strategy_mode, export=args.export, as_json=args.json)
    elif args.command == "kalshi-backtest":
        kalshi_backtest(db_path=args.db_path, strategy=args.strategy, fee_assumption=args.fee_assumption, spread_threshold=args.spread_threshold, bankroll=args.bankroll, export=args.export, as_json=args.json)
    elif args.command == "kalshi-backtest-summary":
        kalshi_backtest_summary(db_path=args.db_path, as_json=args.json)
    elif args.command == "kalshi-record-markets":
        kalshi_record_markets(assets=args.assets, market_types=args.market_types, interval=args.interval, duration_minutes=args.duration_minutes, limit=args.limit, discovery_limit=args.discovery_limit, event_ticker_prefix=args.event_ticker_prefix, ticker_prefix=args.ticker_prefix, db_path=args.db_path, as_json=args.json)
    elif args.command == "kalshi-data-summary":
        kalshi_data_summary(db_path=args.db_path, as_json=args.json)
    elif args.command == "opportunity-ranker":
        opportunity_ranker(venue=args.venue, market_type=args.market_type, asset=args.asset, sport=args.sport, min_roi=args.min_roi, min_confidence=args.min_confidence, max_age_seconds=args.max_age_seconds, limit=args.limit, export=args.export, as_json=args.json)
    elif args.command == "list-sportsbooks":
        list_sportsbooks(as_json=args.json)
    elif args.command == "fetch-odds":
        fetch_odds(args.sport_key, bookmaker=args.bookmaker, region=args.region, markets=args.markets, as_json=args.json)
    elif args.command == "fetch-player-props":
        fetch_player_props(args.sport_key, event_id=args.event_id, bookmaker=args.bookmaker, region=args.region, markets=args.markets, as_json=args.json)
    elif args.command == "oddsblaze-odds":
        fetch_oddsblaze_odds(args.sportsbook, args.league, market=args.market, market_contains=args.market_contains, main=_parse_bool(args.main), live=_parse_bool(args.live), as_json=args.json)
    elif args.command == "debug-player-props":
        debug_player_props(args.sport_key, event_id=args.event_id, bookmaker=args.bookmaker, region=args.region, markets=args.markets, as_json=args.json)
    elif args.command == "scan-prop-arb":
        scan_prop_arb(args.sport_key, event_id=args.event_id, bookmaker=args.bookmaker, region=args.region, markets=args.markets, provider=args.provider, oddsblaze_sportsbooks=_split_csv_values(args.oddsblaze_sportsbooks), oddsblaze_market_contains=args.oddsblaze_market_contains, bankroll=args.bankroll, min_guaranteed_roi=args.min_roi if args.min_roi is not None else args.min_guaranteed_roi, min_profit=args.min_profit, max_leg_age_seconds=args.max_leg_age_seconds, max_cross_leg_update_gap_seconds=args.max_cross_leg_update_gap_seconds, as_json=args.json, profile=args.profile, sportsbooks=args.sportsbooks, db_path=args.db_path, record_analytics=True, summary_json=args.summary_json)
    elif args.command == "watch-prop-arb":
        watch_prop_arb(args.sport_key, event_id=args.event_id, bookmaker=args.bookmaker, region=args.region, markets=args.markets, provider=args.provider, oddsblaze_sportsbooks=_split_csv_values(args.oddsblaze_sportsbooks), oddsblaze_market_contains=args.oddsblaze_market_contains, interval=args.interval, bankroll=args.bankroll, min_roi=args.min_roi, min_profit=args.min_profit, max_leg_age_seconds=args.max_leg_age_seconds, max_cross_leg_update_gap_seconds=args.max_cross_leg_update_gap_seconds, once=args.once, as_json=args.json, db_path=args.db_path, record_analytics=True)
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
        recent_prop_opportunities(limit=args.limit, db_path=args.db_path if args.db_path != "data/polylens.db" else "data/opportunities.db", as_json=args.json)
    elif args.command == "recent-alerts":
        recent_prop_alerts(limit=args.limit, db_path=args.db_path if args.db_path != "data/polylens.db" else "data/opportunities.db", as_json=args.json)
    elif args.command == "opportunity-stats":
        prop_stats(db_path=args.db_path if args.db_path != "data/polylens.db" else "data/opportunities.db", as_json=args.json)
    elif args.command == "opportunity-leaderboard":
        opportunity_leaderboard(db_path=args.db_path, limit=args.limit, as_json=args.json)
    elif args.command == "opportunity-lifetimes":
        opportunity_lifetimes(db_path=args.db_path, limit=args.limit, as_json=args.json)
    elif args.command == "opportunity-quality-report":
        opportunity_quality_report(db_path=args.db_path, as_json=args.json)
    elif args.command == "telegram-test-alert":
        telegram_test_alert(as_json=args.json, db_path=args.db_path)
    elif args.command == "risk-status":
        risk_status(db_path=args.db_path, as_json=args.json)
    elif args.command == "risk-events":
        risk_events(limit=args.limit, db_path=args.db_path, as_json=args.json)
    elif args.command == "risk-halt":
        risk_halt(reason=args.reason, venue=args.venue, db_path=args.db_path, as_json=args.json)
    elif args.command == "risk-resume":
        risk_resume(venue=args.venue, db_path=args.db_path, as_json=args.json)
    elif args.command == "scan-short-crypto":
        assets = [item.strip().upper() for item in args.assets.split(",") if item.strip()]
        windows = [int(item.strip()) for item in args.windows.split(",") if item.strip()]
        _scan_short_crypto(assets=assets, windows=windows, as_json=args.json)
    elif args.command == "watch-short-crypto":
        _watch_short_crypto(paper=args.paper, interval=args.interval, max_loops=args.max_loops, as_json=args.json)
    elif args.command == "trade-short-crypto":
        assets = [item.strip().upper() for item in args.assets.split(",") if item.strip()]
        windows = [int(item.strip()) for item in args.windows.split(",") if item.strip()]
        _trade_short_crypto(
            as_json=args.json,
            paper=args.paper and not args.live and not args.dry_run_live,
            live=args.live,
            max_loops=args.max_loops,
            venue=args.venue,
            assets=assets,
            windows=windows,
            dry_run_live=args.dry_run_live,
        )
    elif args.command == "short-crypto-paper-run":
        from src.analysis.short_crypto_paper import PaperConfig, run_paper

        def _paper_csv_env(arg_value: str | None, env_name: str) -> str | None:
            return arg_value if arg_value is not None else os.environ.get(env_name)

        def _paper_float_env(arg_value: float | None, env_name: str) -> float | None:
            if arg_value is not None:
                return arg_value
            raw = os.environ.get(env_name)
            if raw in {None, ""}:
                return None
            return float(raw)

        directions_raw = _paper_csv_env(args.directions, "POLYLENS_PAPER_DIRECTIONS")
        directions = tuple(item.strip().lower() for item in directions_raw.split(",") if item.strip()) if directions_raw else None
        require_volatility = args.require_volatility_above_median or _env_bool("POLYLENS_PAPER_REQUIRE_VOLATILITY_ABOVE_MEDIAN")
        config = PaperConfig(
            venues=[item.strip().lower() for item in args.venues.split(",") if item.strip()],
            assets=[item.strip().upper() for item in args.assets.split(",") if item.strip()],
            windows=[int(item.strip()) for item in args.windows.split(",") if item.strip()],
            max_trades=args.max_trades,
            max_paper_exposure=args.max_paper_exposure,
            min_edge=args.min_edge,
            min_liquidity=args.min_liquidity,
            freshness_seconds=args.freshness_seconds,
            **({"max_market_lead_time_minutes": args.max_market_lead_time_minutes} if args.max_market_lead_time_minutes is not None else {}),
            db_path=args.db_path,
            discover_only=args.discover_only,
            directions=directions,
            max_model_probability=_paper_float_env(args.max_model_probability, "POLYLENS_PAPER_MAX_MODEL_PROBABILITY"),
            max_entry_price=_paper_float_env(args.max_entry_price, "POLYLENS_PAPER_MAX_ENTRY_PRICE"),
            min_entry_price=_paper_float_env(args.min_entry_price, "POLYLENS_PAPER_MIN_ENTRY_PRICE"),
            require_volatility_above_median=require_volatility,
            strategy_label=_paper_csv_env(args.strategy_label, "POLYLENS_PAPER_STRATEGY_LABEL"),
        )
        result = run_paper(config)
        if args.json:
            print(json.dumps(result, indent=2, sort_keys=True))
        else:
            print(result)
    elif args.command == "short-crypto-paper-settle":
        from src.analysis.short_crypto_paper import settle_due

        result = settle_due(args.db_path)
        if args.json:
            print(json.dumps(result, indent=2, sort_keys=True))
        else:
            print(result)
    elif args.command == "short-crypto-paper-report":
        from src.analysis.short_crypto_diagnostics import verbose_report_extensions
        from src.analysis.short_crypto_paper import performance_report

        result = performance_report(args.db_path)
        if args.verbose:
            result.update(verbose_report_extensions(args.db_path, refresh_features=True))
        if args.json:
            print(json.dumps(result, indent=2, sort_keys=True))
        else:
            print(result)
    elif args.command == "strategy-feedback":
        from src.analysis.strategy_feedback import strategy_feedback_report

        result = strategy_feedback_report(
            args.db_path,
            min_trades=args.min_trades,
            max_adjustment=args.max_adjustment,
        )
        if args.json:
            print(json.dumps(result, indent=2, sort_keys=True))
        else:
            print(result)
    elif args.command == "strategy-recommendations":
        from src.analysis.strategy_recommendations import strategy_recommendations_report

        result = strategy_recommendations_report(
            args.db_path,
            min_trades=args.min_trades,
            max_adjustment=args.max_adjustment,
        )
        if args.json:
            print(json.dumps(result, indent=2, sort_keys=True))
        else:
            print(result)
    elif args.command == "short-crypto-feedback-loop":
        from src.analysis.short_crypto_feedback_loop import short_crypto_feedback_loop

        result = short_crypto_feedback_loop(
            args.db_path,
            min_trades=args.min_trades,
            max_adjustment=args.max_adjustment,
        )
        if args.json:
            print(json.dumps(result, indent=2, sort_keys=True))
        else:
            print(result)
    elif args.command == "short-crypto-paper-diagnostics":
        from src.analysis.short_crypto_diagnostics import diagnostics_report

        result = diagnostics_report(args.db_path, refresh_features=not args.no_refresh)
        if args.json:
            print(json.dumps(result, indent=2, sort_keys=True))
        else:
            print(result)
    elif args.command == "short-crypto-paper-calibration":
        from src.analysis.short_crypto_diagnostics import calibration_report, edge_discovery_report, recommendation_report

        result = {
            **calibration_report(args.db_path, refresh_features=not args.no_refresh),
            **edge_discovery_report(
                args.db_path,
                refresh_features=False,
                min_segment_trades=args.min_segment_trades,
            ),
            **recommendation_report(
                args.db_path,
                refresh_features=False,
                min_segment_trades=args.min_segment_trades,
            ),
        }
        if args.json:
            print(json.dumps(result, indent=2, sort_keys=True))
        else:
            print(result)
    elif args.command == "short-crypto-paper-settlement-audit":
        from src.analysis.polymarket_short_crypto_settlement import settlement_audit

        result = settlement_audit(args.db_path)
        if args.json:
            print(json.dumps(result, indent=2, sort_keys=True))
        else:
            print(result)
    elif args.command == "live-readiness-short-crypto":
        _live_readiness_short_crypto(as_json=args.json)
    elif args.command == "live-readiness-polymarket":
        _live_readiness_polymarket(as_json=args.json)
    elif args.command == "polymarket-auth-audit":
        _polymarket_auth_audit(as_json=args.json)
    elif args.command == "polymarket-credentials-setup":
        _polymarket_credentials_setup(as_json=args.json, write_env=not args.no_write_env)
    elif args.command == "polymarket-tradable-crypto-discovery":
        _polymarket_tradable_crypto_discovery(as_json=args.json)
    elif args.command == "polymarket-event-slug-audit":
        _polymarket_event_slug_audit(slug=args.slug, as_json=args.json)
    elif args.command == "web-dashboard":
        from src.web.app import run_web_dashboard

        run_web_dashboard(host=args.host, port=args.port)


if __name__ == "__main__":
    try:
        main()
    except (MissingOddsAPIKey, MissingOddsBlazeKey, MissingWebhookURLError) as exc:
        raise SystemExit(str(exc)) from exc
