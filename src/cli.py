from __future__ import annotations

import argparse
import logging
from pathlib import Path

from src.adapters.kalshi import KalshiClient
from src.adapters.polymarket import PolymarketClient
from src.analysis.arb_signals import detect_signals
from src.analysis.cross_market import compare_wallet_markets_to_kalshi
from src.analysis.markets import summarize_markets
from src.analysis.pnl import summarize_pnl
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


def build_wallet_report(wallet: str, include_kalshi: bool = False) -> WalletReport:
    logger = logging.getLogger(__name__)
    client = PolymarketClient(raw_dir="data/raw")
    logger.info("starting wallet analysis wallet=%s include_kalshi=%s", wallet, include_kalshi)
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
    cross_platform_candidates = compare_kalshi_candidates(trades) if include_kalshi else []

    limitations = [
        "PnL is estimated from currently available position fields and may omit resolved markets no longer returned by the positions endpoint.",
        "Kalshi arbitrage detection is heuristic and compares public market text only; it does not prove executable arbitrage.",
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
        limitations=limitations,
    )
    logger.info("analysis complete wallet=%s classification=%s kalshi_candidates=%s", wallet, report.behavior_classification, len(cross_platform_candidates))
    return report


def compare_kalshi_candidates(trades: list[dict]) -> list[dict]:
    logger = logging.getLogger(__name__)
    kalshi_client = KalshiClient(raw_dir="data/raw")
    try:
        kalshi_markets = kalshi_client.get_markets(status="open")
    except Exception as exc:
        logger.warning("Kalshi market ingestion failed; returning no candidates: %s", exc)
        return []
    candidates = compare_wallet_markets_to_kalshi(trades, kalshi_markets)
    logger.info("cross-market comparison candidates=%s kalshi_markets=%s", len(candidates), len(kalshi_markets))
    return candidates


def analyze_wallet(wallet: str) -> WalletReport:
    report = build_wallet_report(wallet)
    output = report.save("data/reports")
    logging.getLogger(__name__).info("saved report %s", output)
    print(report.summary_text())
    print(f"\nSaved JSON report: {output}")
    return report


def export_wallet(wallet: str, include_kalshi: bool = False) -> WalletReport:
    report = build_wallet_report(wallet, include_kalshi=include_kalshi)
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


def main() -> None:
    setup_logging()
    parser = argparse.ArgumentParser(prog="polylens")
    sub = parser.add_subparsers(dest="command", required=True)

    wallet_parser = sub.add_parser("analyze-wallet")
    wallet_parser.add_argument("wallet")

    export_parser = sub.add_parser("export-wallet")
    export_parser.add_argument("wallet")
    export_parser.add_argument("--include-kalshi", action="store_true", help="include conservative Polymarket/Kalshi market-overlap candidates")

    compare_parser = sub.add_parser("compare-kalshi")
    compare_parser.add_argument("wallet")

    args = parser.parse_args()

    if args.command == "analyze-wallet":
        analyze_wallet(args.wallet)
    elif args.command == "export-wallet":
        export_wallet(args.wallet, include_kalshi=args.include_kalshi)
    elif args.command == "compare-kalshi":
        compare_kalshi(args.wallet)


if __name__ == "__main__":
    main()
