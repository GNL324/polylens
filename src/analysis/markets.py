from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any

from src.analysis.volume import money_value

CATEGORY_KEYWORDS = {
    "Sports": ["nba", "nfl", "mlb", "nhl", "soccer", "champions league", "ufc", "tennis", "golf", "sports", "super bowl", "world cup"],
    "Crypto": ["bitcoin", "btc", "ethereum", "eth", "solana", "sol", "xrp", "crypto", "coin", "binance"],
    "Politics": ["election", "president", "senate", "congress", "trump", "biden", "democrat", "republican", "government"],
    "Economics": ["fed", "inflation", "cpi", "rate", "recession", "jobs", "unemployment", "gdp"],
    "News": ["war", "ceasefire", "court", "resign", "announce", "deadline", "breaking"],
    "Culture": ["oscar", "grammy", "movie", "album", "tv", "celebrity", "award"],
}


def infer_category(item: dict[str, Any]) -> str:
    text = " ".join(str(item.get(key) or "") for key in ("title", "slug", "eventSlug", "category")).lower()
    for category, keywords in CATEGORY_KEYWORDS.items():
        if any(keyword in text for keyword in keywords):
            return category
    return str(item.get("category") or "Other")


def summarize_markets(trades: list[dict[str, Any]], positions: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    market_volume: dict[str, float] = defaultdict(float)
    market_titles: dict[str, str] = {}
    category_counts: Counter[str] = Counter()
    category_volume: Counter[str] = Counter()

    for trade in trades:
        key = str(trade.get("conditionId") or trade.get("slug") or trade.get("title") or "unknown")
        value = money_value(trade)
        market_volume[key] += value
        market_titles[key] = str(trade.get("title") or trade.get("slug") or key)
        category = infer_category(trade)
        category_counts[category] += 1
        category_volume[category] += value

    total_volume = sum(market_volume.values())
    ranked_markets = sorted(market_volume.items(), key=lambda item: item[1], reverse=True)
    favorite_markets = [
        {"market": market_titles[key], "volume": round(volume, 2), "share": round(volume / total_volume, 4) if total_volume else 0}
        for key, volume in ranked_markets[:10]
    ]
    favorite_categories = [
        {"category": category, "trade_count": count, "volume": round(category_volume[category], 2)}
        for category, count in category_counts.most_common()
    ]
    top_share = ranked_markets[0][1] / total_volume if ranked_markets and total_volume else 0
    concentration = "High" if top_share >= 0.5 else "Medium" if top_share >= 0.25 else "Low"
    return {
        "favorite_markets": favorite_markets,
        "favorite_categories": favorite_categories,
        "market_count": len(market_volume),
        "market_concentration": concentration,
        "top_market_share": round(top_share, 4),
        "open_position_count": len(positions or []),
    }
