from __future__ import annotations

import argparse
import json

from src.analysis.short_crypto_paper import PaperConfig, run_paper


def parse_csv(value: str) -> list[str]:
    return [item.strip().lower() for item in value.split(",") if item.strip()]


def parse_assets(value: str) -> list[str]:
    return [item.strip().upper() for item in value.split(",") if item.strip()]


def parse_windows(value: str) -> list[int]:
    return [int(item.strip()) for item in value.split(",") if item.strip()]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--venues", default="kalshi,polymarket")
    parser.add_argument("--assets", default="BTC,ETH,SOL")
    parser.add_argument("--windows", default="5,10,15")
    parser.add_argument("--max-trades", type=int, default=10)
    parser.add_argument("--max-paper-exposure", type=float, default=100.0)
    parser.add_argument("--min-edge", type=float, default=0.01)
    parser.add_argument("--min-liquidity", type=float, default=1.0)
    parser.add_argument("--freshness-seconds", type=float, default=30.0)
    parser.add_argument("--db-path", default="data/short_crypto_paper.db")
    parser.add_argument("--discover-only", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    result = run_paper(
        PaperConfig(
            venues=parse_csv(args.venues),
            assets=parse_assets(args.assets),
            windows=parse_windows(args.windows),
            max_trades=args.max_trades,
            max_paper_exposure=args.max_paper_exposure,
            min_edge=args.min_edge,
            min_liquidity=args.min_liquidity,
            freshness_seconds=args.freshness_seconds,
            db_path=args.db_path,
            discover_only=args.discover_only,
        )
    )
    print(json.dumps(result, indent=2, sort_keys=True) if args.json else result)


if __name__ == "__main__":
    main()
