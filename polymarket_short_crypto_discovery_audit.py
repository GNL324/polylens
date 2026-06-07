from __future__ import annotations

import argparse
import json
import logging

from src.adapters.polymarket_live import enumerate_short_crypto_discovery_audit


def main() -> None:
    logging.disable(logging.CRITICAL)
    parser = argparse.ArgumentParser(description="Polymarket short-crypto discovery audit")
    parser.add_argument("--json", action="store_true", default=True)
    args = parser.parse_args()
    payload = enumerate_short_crypto_discovery_audit()
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True, default=str))
    else:
        print(f"diagnosis: {payload['diagnosis']} candidates={payload['candidate_count']}")


if __name__ == "__main__":
    main()
