from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any

from src.analysis.markets import infer_category
from src.analysis.volume import money_value

LABELS = [
    "Arbitrage Trader",
    "Sports Arb Trader",
    "Market Maker",
    "Whale",
    "Directional Trader",
    "News Trader",
    "Sports Specialist",
    "Crypto Specialist",
    "Mixed Strategy",
]


def classify_behavior(trades: list[dict[str, Any]], positions: list[dict[str, Any]], market_summary: dict[str, Any], volume_summary: dict[str, Any]) -> dict[str, Any]:
    trade_count = int(volume_summary.get("trade_count") or 0)
    total_volume = float(volume_summary.get("total_volume") or 0)
    avg_trade = float(volume_summary.get("average_trade_size") or 0)
    category_counts = Counter(infer_category(trade) for trade in trades)
    side_counts = Counter(str(trade.get("side") or "").upper() for trade in trades)
    outcomes_by_market: dict[str, set[str]] = defaultdict(set)
    sides_by_market: dict[str, set[str]] = defaultdict(set)
    for trade in trades:
        market = str(trade.get("conditionId") or trade.get("slug") or trade.get("title") or "unknown")
        outcomes_by_market[market].add(str(trade.get("outcome") or "unknown"))
        sides_by_market[market].add(str(trade.get("side") or "unknown").upper())

    hedged_markets = sum(1 for outcomes in outcomes_by_market.values() if len(outcomes) > 1)
    two_sided_markets = sum(1 for sides in sides_by_market.values() if "BUY" in sides and "SELL" in sides)
    market_count = max(1, int(market_summary.get("market_count") or 0))
    sports_share = category_counts["Sports"] / trade_count if trade_count else 0
    crypto_share = category_counts["Crypto"] / trade_count if trade_count else 0
    news_share = category_counts["News"] / trade_count if trade_count else 0
    top_market_share = float(market_summary.get("top_market_share") or 0)
    buy_share = side_counts["BUY"] / trade_count if trade_count else 0

    signals = {
        "possible_kalshi_arbitrage": hedged_markets >= 2 or (sports_share >= 0.5 and two_sided_markets >= 1),
        "hedging_behavior": hedged_markets > 0,
        "market_making": trade_count >= 50 and two_sided_markets / market_count >= 0.25,
        "whale_following": avg_trade >= 1000 or total_volume >= 100000,
        "directional_trading": buy_share >= 0.8 and top_market_share >= 0.35,
    }

    if signals["market_making"]:
        label = "Market Maker"
        confidence = 0.78
    elif signals["possible_kalshi_arbitrage"] and sports_share >= 0.4:
        label = "Sports Arb Trader"
        confidence = 0.72
    elif signals["possible_kalshi_arbitrage"]:
        label = "Arbitrage Trader"
        confidence = 0.66
    elif signals["whale_following"]:
        label = "Whale"
        confidence = 0.7
    elif sports_share >= 0.6:
        label = "Sports Specialist"
        confidence = 0.74
    elif crypto_share >= 0.6:
        label = "Crypto Specialist"
        confidence = 0.74
    elif news_share >= 0.45:
        label = "News Trader"
        confidence = 0.62
    elif signals["directional_trading"]:
        label = "Directional Trader"
        confidence = 0.68
    else:
        label = "Mixed Strategy"
        confidence = 0.5 if trade_count else 0.0

    return {
        "behavior_classification": label,
        "confidence_score": round(confidence, 2),
        "signals": signals,
        "label_candidates": LABELS,
        "hedged_market_count": hedged_markets,
        "two_sided_market_count": two_sided_markets,
    }


def detect_signals(trades: list[dict[str, Any]], positions: list[dict[str, Any]], market_summary: dict[str, Any], volume_summary: dict[str, Any]) -> dict[str, Any]:
    return classify_behavior(trades, positions, market_summary, volume_summary)
