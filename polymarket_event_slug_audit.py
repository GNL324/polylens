from __future__ import annotations

import argparse
import json
import logging
import sys

from src.adapters.polymarket_live import audit_polymarket_event_slug


def main(argv: list[str] | None = None) -> None:
    logging.disable(logging.CRITICAL)
    parser = argparse.ArgumentParser(description="Audit Polymarket event slug resolution")
    parser.add_argument("slug", help="Event slug, e.g. btc-updown-5m-1780859700")
    parser.add_argument("--json", action="store_true", default=True)
    args = parser.parse_args(argv)
    payload = audit_polymarket_event_slug(args.slug)
    print(json.dumps(payload, indent=2, sort_keys=True, default=str))


if __name__ == "__main__":
    main(sys.argv[1:])
