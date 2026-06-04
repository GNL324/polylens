from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from typing import Any

from src.storage.kalshi_market_data import DEFAULT_KALSHI_DATA_DB, KalshiMarketDataStore

CRYPTO_ASSETS = ("BTC", "ETH", "SOL", "SOLANA", "XRP", "DOGE", "LTC", "ADA", "AVAX")


def record_kalshi_markets(
    client: Any,
    *,
    assets: set[str] | None = None,
    market_types: set[str] | None = None,
    interval: int = 60,
    duration_minutes: float | None = None,
    limit: int = 100,
    discovery_limit: int = 1000,
    event_ticker_prefix: str | None = None,
    ticker_prefix: str | None = None,
    db_path: str = DEFAULT_KALSHI_DATA_DB,
    include_orderbooks: bool = True,
) -> dict[str, Any]:
    store = KalshiMarketDataStore(db_path)
    started = time.time()
    deadline = started + (duration_minutes * 60 if duration_minutes else 0)
    iterations = 0
    markets_seen = 0
    orderbooks_saved = 0
    while True:
        iterations += 1
        timestamp = datetime.now(timezone.utc).isoformat()
        raw_markets = _discover_markets(client, discovery_limit=discovery_limit, assets=assets)
        markets = [_normalize_market(market, timestamp) for market in raw_markets]
        markets = [market for market in markets if _passes_filters(market, assets, market_types, event_ticker_prefix, ticker_prefix)]
        for market in markets[:limit]:
            store.save_market_snapshot(market)
            store.save_price_point(_price_point(market))
            markets_seen += 1
            if include_orderbooks:
                try:
                    orderbook = _normalize_orderbook(client.get_orderbook(market["ticker"]), market["ticker"], timestamp)
                except Exception as exc:
                    orderbook = {"timestamp": timestamp, "ticker": market["ticker"], "raw_json": json.dumps({"error": str(exc)}, sort_keys=True)}
                store.save_orderbook_snapshot(orderbook)
                orderbooks_saved += 1
        if not duration_minutes or time.time() >= deadline:
            break
        sleep_for = max(0, min(interval, deadline - time.time()))
        if sleep_for:
            time.sleep(sleep_for)
    summary = store.summary()
    summary["last_recorded_tickers"] = store.recent_tickers(limit=limit)
    return {
        "db_path": str(store.db_path),
        "iterations": iterations,
        "raw_markets_inspected_per_iteration": min(max(int(discovery_limit), 1), max(int(discovery_limit), 1)),
        "market_snapshots_saved": markets_seen,
        "orderbook_snapshots_saved": orderbooks_saved,
        "summary": summary,
    }


def summarize_kalshi_market_data(db_path: str = DEFAULT_KALSHI_DATA_DB) -> dict[str, Any]:
    return KalshiMarketDataStore(db_path).summary()


def _normalize_market(market: dict[str, Any], timestamp: str) -> dict[str, Any]:
    identity = market.get("market_identity") or {}
    pricing = market.get("pricing") or {}
    ticker = market.get("ticker") or identity.get("ticker") or "unknown"
    yes_bid = _price(pricing.get("yes_bid") or pricing.get("best_bid") or market.get("yes_bid") or market.get("yes_bid_dollars"))
    yes_ask = _price(pricing.get("yes_ask") or pricing.get("best_ask") or market.get("yes_ask") or market.get("yes_ask_dollars"))
    no_bid = _price(pricing.get("no_bid") or market.get("no_bid") or market.get("no_bid_dollars"))
    no_ask = _price(pricing.get("no_ask") or market.get("no_ask") or market.get("no_ask_dollars"))
    spread = _spread(yes_bid, yes_ask)
    title = market.get("title") or identity.get("title") or ""
    row = {
        "timestamp": timestamp,
        "ticker": ticker,
        "event_ticker": market.get("event_ticker") or identity.get("event_ticker"),
        "asset": _asset({**market, "title": title}),
        "market_type": _market_type({**market, "title": title}),
        "title": title,
        "status": market.get("status") or identity.get("status"),
        "yes_bid": yes_bid,
        "yes_ask": yes_ask,
        "no_bid": no_bid,
        "no_ask": no_ask,
        "spread": spread,
        "volume": _money(market.get("volume") or market.get("volume_dollars")),
        "liquidity": _money(pricing.get("liquidity") or market.get("liquidity") or market.get("liquidity_dollars")),
        "close_time": market.get("close_time") or identity.get("close_time"),
        "expiration_time": market.get("expiration_time") or identity.get("expiration_time") or market.get("expected_expiration_time"),
        "raw_json": json.dumps(_compact_raw(market), sort_keys=True),
    }
    return row


def _normalize_orderbook(payload: dict[str, Any], ticker: str, timestamp: str) -> dict[str, Any]:
    book = payload.get("orderbook", payload) if isinstance(payload, dict) else {}
    yes_bid, yes_bid_size = _best_level(book.get("yes") or book.get("yes_bids") or book.get("yes_bid"), reverse=True)
    yes_ask, yes_ask_size = _best_level(book.get("yes_asks") or book.get("yes_ask") or book.get("yes"), reverse=False)
    no_bid, no_bid_size = _best_level(book.get("no") or book.get("no_bids") or book.get("no_bid"), reverse=True)
    no_ask, no_ask_size = _best_level(book.get("no_asks") or book.get("no_ask") or book.get("no"), reverse=False)
    return {"timestamp": timestamp, "ticker": ticker, "yes_bid": yes_bid, "yes_bid_size": yes_bid_size, "yes_ask": yes_ask, "yes_ask_size": yes_ask_size, "no_bid": no_bid, "no_bid_size": no_bid_size, "no_ask": no_ask, "no_ask_size": no_ask_size, "spread": _spread(yes_bid, yes_ask), "raw_json": json.dumps(payload, sort_keys=True)}


def _price_point(market: dict[str, Any]) -> dict[str, Any]:
    yes_bid = market.get("yes_bid")
    yes_ask = market.get("yes_ask")
    mid = round((yes_bid + yes_ask) / 2, 4) if yes_bid is not None and yes_ask is not None else None
    return {"timestamp": market.get("timestamp"), "ticker": market.get("ticker"), "asset": market.get("asset"), "market_type": market.get("market_type"), "yes_bid": yes_bid, "yes_ask": yes_ask, "no_bid": market.get("no_bid"), "no_ask": market.get("no_ask"), "mid_price": mid, "spread": market.get("spread"), "liquidity": market.get("liquidity")}


def _discover_markets(client: Any, discovery_limit: int, assets: set[str] | None = None) -> list[dict[str, Any]]:
    discovery_limit = max(int(discovery_limit), 1)
    page_size = min(discovery_limit, 1000)
    max_pages = max(1, (discovery_limit + page_size - 1) // page_size)
    markets: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add_rows(rows: list[dict[str, Any]]) -> None:
        for row in rows:
            ticker = str(row.get("ticker") or "")
            if ticker and ticker not in seen:
                seen.add(ticker)
                markets.append(row)

    for series in _series_tickers_for_assets(assets):
        try:
            add_rows(client.get_markets(status="open", limit=page_size, max_pages=max_pages, series_ticker=series))
        except TypeError:
            break
    if len(markets) < discovery_limit:
        add_rows(client.get_markets(status="open", limit=page_size, max_pages=max_pages))
    return markets[:discovery_limit]


def _series_tickers_for_assets(assets: set[str] | None) -> list[str]:
    if not assets:
        return []
    mapping = {"BTC": ["KXBTC"], "ETH": ["KXETH"], "SOL": ["KXSOL", "KXSOLANA"]}
    series: list[str] = []
    for asset in sorted({item.upper() for item in assets}):
        series.extend(mapping.get(asset, []))
    return series


def _passes_filters(row: dict[str, Any], assets: set[str] | None, market_types: set[str] | None, event_ticker_prefix: str | None = None, ticker_prefix: str | None = None) -> bool:
    ticker = str(row.get("ticker") or "").upper()
    event_ticker = str(row.get("event_ticker") or "").upper()
    if ticker_prefix and not ticker.startswith(ticker_prefix.upper()):
        return False
    if event_ticker_prefix and not event_ticker.startswith(event_ticker_prefix.upper()):
        return False
    if assets:
        if ticker.startswith("KXMVE") or event_ticker.startswith("KXMVE"):
            return False
        if str(row.get("asset", "")).upper() not in assets:
            return False
        if not _has_asset_prefix(ticker, event_ticker, str(row.get("asset", "")).upper()):
            return False
    if market_types and str(row.get("market_type", "")).lower() not in {item.lower() for item in market_types}:
        return False
    return True


def _has_asset_prefix(ticker: str, event_ticker: str, asset: str) -> bool:
    prefixes = {
        "BTC": ("KXBTC",),
        "ETH": ("KXETH",),
        "SOL": ("KXSOL", "KXSOLANA"),
    }.get(asset, ())
    return bool(prefixes and (ticker.startswith(prefixes) or event_ticker.startswith(prefixes)))


def _asset(row: dict[str, Any]) -> str:
    text = " ".join(str(row.get(key) or "") for key in ("asset", "ticker", "event_ticker", "title", "subtitle")).upper()
    prefix_map = {"KXBTC": "BTC", "KXETH": "ETH", "KXSOL": "SOL", "KXSOLANA": "SOL"}
    for prefix, asset in prefix_map.items():
        if prefix in text:
            return asset
    for symbol in CRYPTO_ASSETS:
        if symbol in text or f"KX{symbol}" in text:
            return "SOL" if symbol == "SOLANA" else symbol
    if "BITCOIN" in text:
        return "BTC"
    if "ETHEREUM" in text:
        return "ETH"
    if "SOLANA" in text:
        return "SOL"
    return "unknown"


def _market_type(row: dict[str, Any]) -> str:
    text = " ".join(str(row.get(key) or "") for key in ("market_type", "category", "title", "ticker", "event_ticker")).lower()
    if any(token in text for token in ("btc", "eth", "sol", "xrp", "doge", "bitcoin", "ethereum", "crypto")):
        return "crypto"
    if any(token in text for token in ("nba", "nfl", "mlb", "nhl", "championship", "finals", "sports")):
        return "sports"
    return "unknown"


def _price(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    if numeric > 1 and numeric <= 100:
        numeric /= 100.0
    return round(numeric, 4)


def _money(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    if float(numeric).is_integer() and abs(numeric) > 100:
        numeric /= 100.0
    return round(numeric, 4)


def _spread(bid: float | None, ask: float | None) -> float | None:
    if bid is None or ask is None:
        return None
    return round(max(ask - bid, 0), 4)


def _best_level(levels: Any, reverse: bool) -> tuple[float | None, float | None]:
    if not levels:
        return None, None
    rows = levels if isinstance(levels, list) else [levels]
    parsed = []
    for row in rows:
        if isinstance(row, dict):
            price = _price(row.get("price") or row.get("yes_price") or row.get("no_price"))
            size = row.get("size") or row.get("count") or row.get("quantity")
        elif isinstance(row, (list, tuple)) and row:
            price = _price(row[0])
            size = row[1] if len(row) > 1 else None
        else:
            continue
        if price is not None:
            parsed.append((price, _money(size)))
    if not parsed:
        return None, None
    parsed.sort(key=lambda item: item[0], reverse=reverse)
    return parsed[0]


def _compact_raw(market: dict[str, Any]) -> dict[str, Any]:
    keys = ("ticker", "event_ticker", "title", "category", "status", "yes_bid", "yes_ask", "no_bid", "no_ask", "liquidity", "volume", "close_time", "expiration_time")
    return {key: market.get(key) for key in keys if key in market}
