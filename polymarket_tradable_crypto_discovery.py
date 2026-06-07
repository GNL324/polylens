from __future__ import annotations

import argparse
import json
import logging

from src.adapters.polymarket_live import build_tradable_crypto_discovery


def main() -> None:
    logging.disable(logging.CRITICAL)
    parser = argparse.ArgumentParser(description="Polymarket tradable crypto discovery")
    parser.add_argument("--json", action="store_true", default=True)
    args = parser.parse_args()
    payload = build_tradable_crypto_discovery()
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True, default=str))
    else:
        summary = payload["summary"]
        print(
            f"tradable crypto discovery: {summary['diagnosis']} "
            f"executable={summary['executable_books']} short_exec={summary['short_crypto_executable_books']}"
        )


if __name__ == "__main__":
    main()
