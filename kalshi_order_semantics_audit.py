from __future__ import annotations

import json
import logging
import sys
from typing import Any

from src.adapters.kalshi import KalshiClient
from src.analysis.short_crypto_executor import build_kalshi_order_semantics


def main() -> None:
    logging.disable(logging.CRITICAL)
    ticker = sys.argv[1] if len(sys.argv) > 1 else ""
    if not ticker:
        print(json.dumps({"status": "error", "reason": "ticker_required"}, indent=2, sort_keys=True))
        return
    book = KalshiClient(raw_dir="data/raw").get_orderbook(ticker)
    print(json.dumps(audit_semantics(ticker, book), indent=2, sort_keys=True, default=str))


def audit_semantics(ticker: str, book: dict[str, Any]) -> dict[str, Any]:
    semantics = build_kalshi_order_semantics(ticker, book)
    return {
        "status": "ok",
        "sent": False,
        "ticker": ticker,
        "yes_ladder": semantics["yes_ladder"],
        "no_ladder": semantics["no_ladder"],
        "best_yes_bid": semantics["best_yes_bid"],
        "best_no_bid": semantics["best_no_bid"],
        "derived_yes_ask_from_no_bid": semantics["derived_yes_ask_from_no_bid"],
        "candidate_payloads_without_sending": semantics["candidate_payloads"],
        "crossing_explanation": {
            "A": "buy yes at derived YES ask. This is the direct buy-YES representation of the complementary NO bid, but recent fill_or_kill_insufficient_resting_volume responses indicate it may not cross the NO bid ladder on Kalshi.",
            "B": "sell yes at best YES bid. This should cross resting YES bid liquidity.",
            "C": "buy no at derived NO ask. This is the direct buy-NO representation of the complementary YES bid, but it may not cross the YES bid ladder if Kalshi only matches against same-side bid ladders via sell actions.",
            "D": "sell no at best NO bid. This should cross resting NO bid liquidity and is the economically equivalent candidate for taking the derived YES ask.",
        },
        "determined_payload_conventions": {
            "buying_yes_by_taking_ask": "Use action=sell, side=no, no_price=best_no_bid to cross the resting NO bid ladder. Treat action=buy, side=yes, yes_price=100-best_no_bid as a non-posting direct candidate only until Kalshi confirms it crosses complementary depth.",
            "selling_yes_into_bid": "Use action=sell, side=yes, yes_price=best_yes_bid to cross the resting YES bid ladder.",
            "buying_no_by_taking_ask": "Use action=sell, side=yes, yes_price=best_yes_bid to cross the resting YES bid ladder. Treat action=buy, side=no, no_price=100-best_yes_bid as a non-posting direct candidate only until Kalshi confirms it crosses complementary depth.",
            "selling_no_into_bid": "Use action=sell, side=no, no_price=best_no_bid to cross the resting NO bid ladder.",
        },
        "safety": {
            "live_sends_stopped": True,
            "order_endpoint_called": False,
        },
    }


if __name__ == "__main__":
    main()
