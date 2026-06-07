from __future__ import annotations

import argparse
import json

from src.analysis.short_crypto_paper import settle_due


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db-path", default="data/short_crypto_paper.db")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    result = settle_due(args.db_path)
    print(json.dumps(result, indent=2, sort_keys=True) if args.json else result)


if __name__ == "__main__":
    main()
