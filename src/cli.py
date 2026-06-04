from __future__ import annotations

import argparse
import json
import logging
import os
import resource
import time
from pathlib import Path
from typing import Any

from src.adapters.kalshi import KalshiAuthenticatedClient, KalshiAuthConfigError, KalshiClient
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
from src.analysis.pnl import summarize_pnl
from src.analysis.prop_arbitrage import scan_prop_arbitrage
from src.analysis.prop_normalization import normalize_player_props
from src.analysis.sportsbook_matching import match_sportsbook_lines
from src.analysis.synthetic_field import debug_synthetic_field as build_debug_synthetic_field
from src.analysis.timing import summarize_timing
from src.analysis.volume import summarize_volume
from src.analysis.watch_mode import watch_live_arbitrage
from src.reports.wallet_report import WalletReport
from src.storage.kalshi_market_data import DEFAULT_KALSHI_DATA_DB
from src.storage.opportunity_store import OpportunityStore
from src.storage.opportunities import load_recent_alerts as load_prop_recent_alerts, load_recent_opportunities as load_prop_recent_opportunities, opportunity_key as prop_opportunity_key, opportunity_stats as prop_opportunity_stats, save_alert as save_prop_alert, save_opportunity as save_prop_opportunity
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


def scan_prop_arb(sport_key: str, event_id: str | None = None, bookmaker: str | None = None, region: str = "us", markets: str | None = None, bankroll: float | None = None, min_guaranteed_roi: float | None = None, min_profit: float | None = None, as_json: bool = False) -> dict[str, Any]:
    started = time.time()
    props = fetch_player_props(sport_key, event_id=event_id, bookmaker=bookmaker, region=region, markets=markets, quiet=True)
    duration = round(time.time() - started, 4)
    result = scan_prop_arbitrage(props, bankroll=bankroll, min_guaranteed_roi=min_guaranteed_roi, min_profit=min_profit, scan_duration_seconds=duration, api_calls=None)
    if as_json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print("Polylens Player Prop Arbitrage")
        print("=" * 31)
        print(f"Props fetched: {result['props_fetched']}")
        print(f"Matched prop pairs: {result['matched_prop_pairs']}")
        print(f"True arb candidates: {len(result['prop_arbitrage_candidates'])}")
        print(f"Rejection reasons: {result['rejection_reasons']}")
    return result


def watch_prop_arb(sport_key: str, event_id: str | None = None, bookmaker: str | None = None, region: str = "us", markets: str | None = None, interval: int = 30, bankroll: float | None = None, min_roi: float | None = None, min_profit: float | None = None, once: bool = False, as_json: bool = False, db_path: str = "data/opportunities.db") -> dict[str, Any]:
    seen: set[str] = set()
    iterations = 0
    alerts_sent = 0
    last_result: dict[str, Any] = {}
    while True:
        iterations += 1
        result = scan_prop_arb(sport_key, event_id=event_id, bookmaker=bookmaker, region=region, markets=markets, bankroll=bankroll, min_guaranteed_roi=min_roi, min_profit=min_profit, as_json=False)
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
    prop_arb_parser.add_argument("--region", default="us")
    prop_arb_parser.add_argument("--markets")
    prop_arb_parser.add_argument("--bankroll", type=float)
    prop_arb_parser.add_argument("--min-guaranteed-roi", type=float)
    prop_arb_parser.add_argument("--min-roi", type=float, dest="min_roi")
    prop_arb_parser.add_argument("--min-profit", type=float)
    prop_arb_parser.add_argument("--json", action="store_true")

    watch_prop_parser = sub.add_parser("watch-prop-arb")
    watch_prop_parser.add_argument("--sport", required=True, dest="sport_key")
    watch_prop_parser.add_argument("--event-id")
    watch_prop_parser.add_argument("--bookmaker")
    watch_prop_parser.add_argument("--region", default="us")
    watch_prop_parser.add_argument("--markets")
    watch_prop_parser.add_argument("--interval", type=int, default=30)
    watch_prop_parser.add_argument("--bankroll", type=float)
    watch_prop_parser.add_argument("--min-roi", type=float)
    watch_prop_parser.add_argument("--min-profit", type=float)
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
    elif args.command == "list-sportsbooks":
        list_sportsbooks(as_json=args.json)
    elif args.command == "fetch-odds":
        fetch_odds(args.sport_key, bookmaker=args.bookmaker, region=args.region, markets=args.markets, as_json=args.json)
    elif args.command == "fetch-player-props":
        fetch_player_props(args.sport_key, event_id=args.event_id, bookmaker=args.bookmaker, region=args.region, markets=args.markets, as_json=args.json)
    elif args.command == "debug-player-props":
        debug_player_props(args.sport_key, event_id=args.event_id, bookmaker=args.bookmaker, region=args.region, markets=args.markets, as_json=args.json)
    elif args.command == "scan-prop-arb":
        scan_prop_arb(args.sport_key, event_id=args.event_id, bookmaker=args.bookmaker, region=args.region, markets=args.markets, bankroll=args.bankroll, min_guaranteed_roi=args.min_roi if args.min_roi is not None else args.min_guaranteed_roi, min_profit=args.min_profit, as_json=args.json)
    elif args.command == "watch-prop-arb":
        watch_prop_arb(args.sport_key, event_id=args.event_id, bookmaker=args.bookmaker, region=args.region, markets=args.markets, interval=args.interval, bankroll=args.bankroll, min_roi=args.min_roi, min_profit=args.min_profit, once=args.once, as_json=args.json, db_path=args.db_path)
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


if __name__ == "__main__":
    try:
        main()
    except (MissingOddsAPIKey, MissingWebhookURLError) as exc:
        raise SystemExit(str(exc)) from exc
