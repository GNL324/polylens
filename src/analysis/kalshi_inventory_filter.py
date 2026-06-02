from __future__ import annotations

from collections import Counter
from typing import Any

from src.analysis.market_normalization import normalize_text
from src.analysis.match_validation import classify_kalshi_product

KEEP_PRODUCT_TYPES = {"outright", "championship_winner", "game_winner", "spread", "total"}
REJECT_PRODUCT_TYPES = {"multileg", "cross_category", "player_prop_bundle", "sportsbook_style_same_game_parlay", "unknown"}
CRYPTO_PREFIXES = ("KXBTC", "KXETH", "KXSOL", "KXDOGE", "KXXRP", "KXLTC", "KXADA", "KXBNB")


def filter_kalshi_inventory(markets: list[dict[str, Any]], sample_limit: int = 20) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    retained: list[dict[str, Any]] = []
    discarded: list[dict[str, Any]] = []
    reason_counts: Counter[str] = Counter()
    retained_sample: list[dict[str, Any]] = []

    for market in markets:
        product_type = classify_kalshi_inventory_product(market)
        summary = _market_summary(market, product_type)
        if product_type in KEEP_PRODUCT_TYPES:
            retained.append(market)
            if len(retained_sample) < sample_limit:
                retained_sample.append(summary)
            continue
        reason = f"rejected {product_type}"
        reason_counts[reason] += 1
        if len(discarded) < sample_limit:
            discarded.append({**summary, "discard_reason": reason})

    diagnostics = {
        "kalshi_markets_fetched": len(markets),
        "kalshi_markets_retained": len(retained),
        "kalshi_markets_discarded": len(markets) - len(retained),
        "discarded_reason_counts": dict(reason_counts),
        "retained_sample": retained_sample,
        "discarded_sample": discarded,
    }
    return retained, diagnostics


def classify_kalshi_inventory_product(market: dict[str, Any]) -> str:
    text = _market_text(market)
    ticker = str(market.get("ticker") or market.get("market_ticker") or market.get("event_ticker") or market.get("series_ticker") or "").upper()

    if ticker.startswith("KXMVECROSSCATEGORY") or "crosscategory" in text or "cross category" in text or "cross-category" in text:
        return "cross_category"
    if ticker.startswith("KXMVESPORTSMULTIGAMEEXTENDED") or "same game parlay" in text:
        return "sportsbook_style_same_game_parlay"
    if "player prop bundle" in text or ("player" in text and _has_any(text, ("bundle", "parlay", "multi-leg", "multileg"))):
        return "player_prop_bundle"
    if ticker.startswith("KXMVE") or _has_any(text, ("multigame", "multi game", "multi-leg", "multileg", "parlay", "associated markets", "associated events", "bundle")):
        return "multileg"

    if ticker.startswith(CRYPTO_PREFIXES) or _has_crypto_terms(text):
        return "outright"

    product_type = classify_kalshi_product(market)
    if product_type == "game_total":
        return "total"
    if product_type == "player_prop":
        return "player_prop_bundle"
    if product_type in {"outright", "championship_winner", "game_winner", "spread", "total"}:
        return product_type
    if product_type in {"multileg", "cross_category"}:
        return product_type
    return "unknown"


def _market_summary(market: dict[str, Any], product_type: str) -> dict[str, Any]:
    return {
        "ticker": market.get("ticker") or market.get("market_ticker"),
        "event_ticker": market.get("event_ticker"),
        "title": market.get("title") or market.get("subtitle") or market.get("ticker"),
        "subtitle": market.get("subtitle"),
        "product_type": product_type,
    }


def _market_text(market: dict[str, Any]) -> str:
    fields = (
        "ticker",
        "market_ticker",
        "event_ticker",
        "series_ticker",
        "title",
        "subtitle",
        "yes_sub_title",
        "no_sub_title",
        "rules_primary",
        "rules_secondary",
        "category",
    )
    parts = [str(market.get(field) or "") for field in fields]
    custom = market.get("custom_strike")
    if isinstance(custom, dict):
        parts.extend(str(value or "") for value in custom.values())
    return f" {normalize_text(' '.join(parts))} "


def _has_any(text: str, terms: tuple[str, ...]) -> bool:
    return any(normalize_text(term) in text for term in terms)


def _has_crypto_terms(text: str) -> bool:
    word_terms = ("bitcoin", "btc", "ethereum", "eth", "solana", "sol", "dogecoin", "doge", "xrp")
    return any(f" {normalize_text(term)} " in text for term in word_terms)
