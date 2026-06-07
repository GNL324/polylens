from __future__ import annotations

import json
import logging
import sys
from typing import Any

from src.adapters.kalshi import KalshiClient
from src.analysis.short_crypto_executor import select_executable_yes_ask


def main() -> None:
    logging.disable(logging.CRITICAL)
    ticker = sys.argv[1] if len(sys.argv) > 1 else ""
    if not ticker:
        print(json.dumps({"status": "error", "reason": "ticker_required"}, indent=2, sort_keys=True))
        return
    book = KalshiClient(raw_dir="data/raw").get_orderbook(ticker)
    print(json.dumps(audit_orderbook(ticker, book), indent=2, sort_keys=True, default=str))


def audit_orderbook(ticker: str, book: dict[str, Any]) -> dict[str, Any]:
    ladder = (book or {}).get("orderbook_fp") or (book or {}).get("orderbook") or (book or {})
    yes_levels = ladder.get("yes_dollars") or ladder.get("yes") or []
    no_levels = ladder.get("no_dollars") or ladder.get("no") or []
    selected = select_executable_yes_ask(book)
    best_yes_bid = _best_bid(yes_levels)
    return {
        "ticker": ticker,
        "raw_orderbook_keys": list((book or {}).keys()),
        "raw_ladder_keys": list(ladder.keys()) if isinstance(ladder, dict) else [],
        "yes_levels": yes_levels,
        "no_levels": no_levels,
        "derived_yes_bid": best_yes_bid["price"] if best_yes_bid else None,
        "derived_yes_ask": selected["price_cents"] / 100.0 if selected.get("ok") else None,
        "derived_yes_ask_count": selected.get("count") if selected.get("ok") else None,
        "selected_no_bid": selected.get("no_bid_cents") / 100.0 if selected.get("ok") else None,
        "selected_no_bid_raw": selected.get("raw") if selected.get("ok") else None,
        "derivation_status": "ok" if selected.get("ok") else selected.get("reason"),
        "explanation": "Kalshi binary orderbooks are bid ladders. YES bid levels are bids to buy YES, not executable YES asks. A marketable YES buy executes against resting NO bids by converting best_no_bid to yes_ask = 1.00 - best_no_bid; ask_count is the size at that NO bid.",
    }


def _best_bid(levels: list[Any]) -> dict[str, Any] | None:
    best = None
    for raw in levels:
        parsed = _parse_level(raw)
        if not parsed:
            continue
        if best is None or parsed["price"] > best["price"]:
            best = parsed
    return best


def _parse_level(raw: Any) -> dict[str, Any] | None:
    try:
        if isinstance(raw, dict):
            price = float(raw.get("price") or raw.get("p"))
            count = int(float(raw.get("count") or raw.get("size") or raw.get("quantity") or raw.get("q")))
        elif isinstance(raw, (list, tuple)) and len(raw) >= 2:
            price = float(raw[0])
            count = int(float(raw[1]))
        else:
            return None
    except Exception:
        return None
    return {"price": price, "count": count, "raw": raw}


if __name__ == "__main__":
    main()
