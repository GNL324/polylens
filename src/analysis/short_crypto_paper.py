from __future__ import annotations

import json
import math
import os
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from src.adapters.kalshi import KalshiClient
from src.adapters.polymarket import PolymarketClient
from src.analysis.short_crypto_markets import ShortCryptoMarket, normalize_short_crypto_markets

DEFAULT_DB_PATH = "data/short_crypto_paper.db"
DEFAULT_SIZE = 1.0
FEE_PLACEHOLDER = 0.0
DEFAULT_MAX_MARKET_LEAD_TIME_MINUTES = 60.0


@dataclass(frozen=True)
class PaperConfig:
    venues: list[str]
    assets: list[str]
    windows: list[int]
    max_trades: int = 10
    max_paper_exposure: float = 100.0
    min_edge: float = 0.01
    min_liquidity: float = 1.0
    freshness_seconds: float = 30.0
    db_path: str = DEFAULT_DB_PATH
    discover_only: bool = False
    max_market_lead_time_minutes: float = field(default_factory=lambda: _float_env("POLYLENS_MAX_MARKET_LEAD_TIME_MINUTES", DEFAULT_MAX_MARKET_LEAD_TIME_MINUTES))


class ShortCryptoPaperStore:
    def __init__(self, db_path: str = DEFAULT_DB_PATH) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    def initialize(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS paper_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    started_at TEXT NOT NULL,
                    completed_at TEXT,
                    venues TEXT NOT NULL,
                    assets TEXT NOT NULL,
                    windows TEXT NOT NULL,
                    max_trades INTEGER NOT NULL,
                    created_trades INTEGER NOT NULL DEFAULT 0,
                    rejected_signals INTEGER NOT NULL DEFAULT 0,
                    raw_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS paper_signals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id INTEGER,
                    created_at TEXT NOT NULL,
                    venue TEXT NOT NULL,
                    asset TEXT NOT NULL,
                    market TEXT,
                    ticker TEXT,
                    slug TEXT,
                    token_id TEXT,
                    direction TEXT NOT NULL,
                    side TEXT NOT NULL,
                    action TEXT NOT NULL,
                    window_minutes INTEGER NOT NULL,
                    entry_time TEXT NOT NULL,
                    end_time TEXT NOT NULL,
                    spot_price REAL,
                    model_probability REAL,
                    implied_probability REAL,
                    edge REAL,
                    size REAL,
                    status TEXT NOT NULL,
                    rejection_reason TEXT,
                    raw_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS paper_trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    signal_id INTEGER NOT NULL,
                    run_id INTEGER,
                    created_at TEXT NOT NULL,
                    venue TEXT NOT NULL,
                    asset TEXT NOT NULL,
                    market TEXT,
                    ticker TEXT,
                    slug TEXT,
                    token_id TEXT,
                    direction TEXT NOT NULL,
                    side TEXT NOT NULL,
                    action TEXT NOT NULL,
                    window_minutes INTEGER NOT NULL,
                    entry_time TEXT NOT NULL,
                    end_time TEXT NOT NULL,
                    entry_price REAL NOT NULL,
                    model_probability REAL NOT NULL,
                    implied_probability REAL NOT NULL,
                    edge REAL NOT NULL,
                    size REAL NOT NULL,
                    paper_cost REAL NOT NULL,
                    fee REAL NOT NULL DEFAULT 0,
                    expected_value REAL NOT NULL,
                    status TEXT NOT NULL,
                    raw_json TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_paper_trades_open_market
                    ON paper_trades(venue, COALESCE(ticker,''), COALESCE(slug,''), COALESCE(token_id,''), direction, window_minutes)
                    WHERE status = 'open';
                CREATE TABLE IF NOT EXISTS paper_settlements (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    trade_id INTEGER NOT NULL UNIQUE,
                    settled_at TEXT NOT NULL,
                    result TEXT NOT NULL,
                    payout REAL,
                    pnl REAL,
                    roi REAL,
                    settlement_source TEXT NOT NULL,
                    raw_json TEXT NOT NULL
                );
                """
            )

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def start_run(self, config: PaperConfig) -> int:
        with self.connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO paper_runs (started_at, venues, assets, windows, max_trades, raw_json)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (_now(), ",".join(config.venues), ",".join(config.assets), ",".join(map(str, config.windows)), config.max_trades, _json(config.__dict__)),
            )
            return int(cur.lastrowid)

    def finish_run(self, run_id: int, *, created_trades: int, rejected_signals: int, meta: dict[str, Any]) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE paper_runs SET completed_at=?, created_trades=?, rejected_signals=?, raw_json=? WHERE id=?",
                (_now(), created_trades, rejected_signals, _json(meta), run_id),
            )

    def save_signal(self, intent: dict[str, Any], *, run_id: int, status: str, reason: str | None = None) -> int:
        with self.connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO paper_signals
                (run_id, created_at, venue, asset, market, ticker, slug, token_id, direction, side, action,
                 window_minutes, entry_time, end_time, spot_price, model_probability, implied_probability,
                 edge, size, status, rejection_reason, raw_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    _now(),
                    intent["venue"],
                    intent["asset"],
                    intent.get("market"),
                    intent.get("ticker"),
                    intent.get("slug"),
                    intent.get("token_id"),
                    intent["direction"],
                    intent["side"],
                    intent["action"],
                    int(intent["window_minutes"]),
                    intent["entry_time"],
                    intent["end_time"],
                    _float_or_none(intent.get("spot_price")),
                    float(intent["model_probability"]),
                    float(intent["implied_probability"]),
                    float(intent["edge"]),
                    float(intent["size"]),
                    status,
                    reason,
                    _json(intent),
                ),
            )
            return int(cur.lastrowid)

    def save_trade(self, intent: dict[str, Any], *, signal_id: int, run_id: int) -> int:
        with self.connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO paper_trades
                (signal_id, run_id, created_at, venue, asset, market, ticker, slug, token_id, direction, side, action,
                 window_minutes, entry_time, end_time, entry_price, model_probability, implied_probability, edge,
                 size, paper_cost, fee, expected_value, status, raw_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?)
                """,
                (
                    signal_id,
                    run_id,
                    _now(),
                    intent["venue"],
                    intent["asset"],
                    intent.get("market"),
                    intent.get("ticker"),
                    intent.get("slug"),
                    intent.get("token_id"),
                    intent["direction"],
                    intent["side"],
                    intent["action"],
                    int(intent["window_minutes"]),
                    intent["entry_time"],
                    intent["end_time"],
                    float(intent["entry_price"]),
                    float(intent["model_probability"]),
                    float(intent["implied_probability"]),
                    float(intent["edge"]),
                    float(intent["size"]),
                    float(intent["paper_cost"]),
                    float(intent.get("fee", 0.0)),
                    float(intent["expected_value"]),
                    _json(intent),
                ),
            )
            return int(cur.lastrowid)

    def has_duplicate_open(self, intent: dict[str, Any]) -> bool:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT 1 FROM paper_trades
                WHERE status='open' AND venue=? AND COALESCE(ticker,'')=? AND COALESCE(slug,'')=?
                  AND COALESCE(token_id,'')=? AND direction=? AND window_minutes=?
                LIMIT 1
                """,
                (
                    intent["venue"],
                    intent.get("ticker") or "",
                    intent.get("slug") or "",
                    intent.get("token_id") or "",
                    intent["direction"],
                    int(intent["window_minutes"]),
                ),
            ).fetchone()
        return row is not None

    def open_exposure(self) -> float:
        with self.connect() as conn:
            value = conn.execute("SELECT COALESCE(SUM(paper_cost), 0) FROM paper_trades WHERE status='open'").fetchone()[0]
        return float(value or 0.0)

    def due_open_trades(self, now_ts: float | None = None) -> list[dict[str, Any]]:
        now_ts = now_ts if now_ts is not None else time.time()
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM paper_trades WHERE status='open' ORDER BY end_time, id").fetchall()
        return [dict(row) for row in rows if _parse_ts(row["end_time"]) <= now_ts]

    def mark_settled(self, trade: dict[str, Any], settlement: dict[str, Any]) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO paper_settlements
                (trade_id, settled_at, result, payout, pnl, roi, settlement_source, raw_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    int(trade["id"]),
                    _now(),
                    settlement["result"],
                    _float_or_none(settlement.get("payout")),
                    _float_or_none(settlement.get("pnl")),
                    _float_or_none(settlement.get("roi")),
                    settlement["settlement_source"],
                    _json(settlement),
                ),
            )
            conn.execute("UPDATE paper_trades SET status=? WHERE id=?", (settlement["result"], int(trade["id"])))


def simulate_fill(intent: dict[str, Any], book: dict[str, Any]) -> dict[str, Any]:
    action = str(intent.get("action", "BUY")).upper()
    side = str(intent.get("side", "YES")).upper()
    best = best_executable_price(book, side=side, action=action)
    if best is None:
        return {"filled": False, "reason": "no_executable_book"}
    size = float(intent.get("size") or DEFAULT_SIZE)
    price = float(best["price"])
    return {
        "filled": True,
        "entry_price": price,
        "size": size,
        "paper_cost": price * size if action == "BUY" else max(0.0, (1.0 - price) * size),
        "fee": FEE_PLACEHOLDER,
        "liquidity": float(best.get("size") or 0.0),
        "liquidity_source": best.get("source"),
    }


def best_executable_price(book: dict[str, Any], *, side: str, action: str) -> dict[str, Any] | None:
    side = side.lower()
    action = action.upper()
    if action == "BUY":
        direct = _best_ask(_levels(book, side, "asks"))
        if direct:
            return direct
        complement = "no" if side == "yes" else "yes"
        comp_bid = _best_bid(_levels(book, complement, "bids"))
        if comp_bid:
            return {"price": round(1.0 - comp_bid["price"], 6), "size": comp_bid.get("size"), "source": f"{complement}_bid_complement"}
        return None
    if action == "SELL":
        return _best_bid(_levels(book, side, "bids"))
    raise ValueError("action must be BUY or SELL")


def run_paper(config: PaperConfig) -> dict[str, Any]:
    store = ShortCryptoPaperStore(config.db_path)
    run_id = store.start_run(config)
    created = 0
    rejected = 0
    errors: list[dict[str, Any]] = []
    counters = _debug_counters()
    counters["max_market_lead_time_minutes"] = float(config.max_market_lead_time_minutes)
    markets = discover_markets(config, errors, counters)
    _finalize_lead_time_stats(counters)
    counters["executable_books_seen"] = sum(1 for market in markets if simulate_fill(build_intent(market, None), build_intent(market, None)["book"])["filled"])
    if config.discover_only:
        result = {
            "run_id": run_id,
            "discover_only": True,
            "markets_seen": len(markets),
            "paper_trades_created": 0,
            "rejected_signals": 0,
            "errors": errors,
            **counters,
        }
        store.finish_run(run_id, created_trades=0, rejected_signals=0, meta=result)
        return result
    spots = fetch_spot_prices(config.assets)
    exposure = store.open_exposure()

    for market in markets:
        intent = build_intent(market, spots.get(market.asset))
        reason = validate_intent(intent, config, store, exposure)
        if reason:
            store.save_signal(intent, run_id=run_id, status="rejected", reason=reason)
            _count_rejection(counters, reason)
            rejected += 1
            continue
        fill = simulate_fill(intent, intent["book"])
        if not fill["filled"]:
            store.save_signal(intent, run_id=run_id, status="rejected", reason=fill["reason"])
            _count_rejection(counters, fill["reason"])
            rejected += 1
            continue
        intent.update(fill)
        intent["expected_value"] = (intent["model_probability"] - intent["entry_price"]) * intent["size"] - intent["fee"]
        signal_id = store.save_signal(intent, run_id=run_id, status="accepted")
        try:
            store.save_trade(intent, signal_id=signal_id, run_id=run_id)
            created += 1
            exposure += float(intent["paper_cost"])
        except sqlite3.IntegrityError:
            rejected += 1
            _count_rejection(counters, "duplicate_market")
            with store.connect() as conn:
                conn.execute("UPDATE paper_signals SET status='rejected', rejection_reason='duplicate_market' WHERE id=?", (signal_id,))
        if created >= config.max_trades:
            break

    result = {
        "run_id": run_id,
        "discover_only": False,
        "markets_seen": len(markets),
        "paper_trades_created": created,
        "rejected_signals": rejected,
        "errors": errors,
        **counters,
    }
    store.finish_run(run_id, created_trades=created, rejected_signals=rejected, meta=result)
    return result


def discover_markets(config: PaperConfig, errors: list[dict[str, Any]] | None = None, counters: dict[str, Any] | None = None) -> list[ShortCryptoMarket]:
    errors = errors if errors is not None else []
    counters = counters if counters is not None else _debug_counters()
    kalshi_rows: list[dict[str, Any]] = []
    polymarket_markets: list[ShortCryptoMarket] = []
    if "kalshi" in config.venues:
        try:
            client = KalshiClient(raw_dir="data/raw")
            rows = client.get_markets(status="open", limit=250, max_pages=1)
            candidates = [
                row for row in rows
                if _looks_like_enabled_kalshi_short_crypto(row, config.assets, config.windows)
            ][: max(config.max_trades * 8, 25)]
            for row in candidates:
                ticker = row.get("ticker") or row.get("market_identity", {}).get("ticker")
                if not ticker:
                    continue
                try:
                    row["orderbook"] = client.get_orderbook(ticker)
                except Exception as exc:
                    row["orderbook_error"] = str(exc)
                kalshi_rows.append(_attach_kalshi_book(row))
        except Exception as exc:
            errors.append({"venue": "kalshi", "error": str(exc)})
    if "polymarket" in config.venues:
        try:
            polymarket_markets = discover_polymarket_series_markets(config, counters)
        except Exception as exc:
            errors.append({"venue": "polymarket", "error": str(exc)})
    kalshi_markets = normalize_short_crypto_markets(kalshi_rows, [])
    counters["kalshi_markets_seen"] = len(kalshi_markets)
    return [m for m in [*kalshi_markets, *polymarket_markets] if m.asset in config.assets and (m.window_minutes in config.windows if m.window_minutes else True)]


def discover_polymarket_series_markets(config: PaperConfig, counters: dict[str, Any] | None = None) -> list[ShortCryptoMarket]:
    counters = counters if counters is not None else _debug_counters()
    try:
        from src.adapters import polymarket_live as live
    except Exception:
        return _discover_polymarket_gamma_fallback(config, counters)

    markets: list[ShortCryptoMarket] = []
    seen: set[tuple[str, str, str]] = set()
    for asset in config.assets:
        for window in config.windows:
            series_slug = live.SHORT_CRYPTO_SERIES_BY_ASSET.get(asset.upper(), {}).get(int(window))
            if not series_slug:
                continue
            series_rows = list(live.series_live_event_markets(series_slug))
            counters["polymarket_markets_seen"] += len(series_rows)
            for row in series_rows:
                parsed = live._parse_market_tokens(row)
                slug = str(row.get("slug") or row.get("marketSlug") or row.get("id") or "")
                timing = market_lead_time(row, max_lead_time_minutes=config.max_market_lead_time_minutes)
                if timing["reason"]:
                    _count_rejection(counters, timing["reason"])
                    _record_market_log(counters, row, timing, accepted=False)
                    continue
                if not parsed:
                    _count_rejection(counters, "token_ids_missing")
                    continue
                eligible, skip_reason = live._short_crypto_market_eligible(row)
                if not eligible:
                    _count_rejection(counters, skip_reason or "market_not_eligible")
                    continue
                for outcome in parsed["outcomes"]:
                    direction = _polymarket_outcome_direction(outcome["name"])
                    if direction is None:
                        _count_rejection(counters, "unsupported_outcome")
                        continue
                    token_id = str(outcome["token_id"])
                    key = (slug, token_id, direction)
                    if key in seen:
                        continue
                    seen.add(key)
                    probe = live._probe_orderbook_with_variants(token_id)
                    if not probe.get("ok"):
                        _count_rejection(counters, probe.get("reason") or "orderbook_fetch_failed")
                        continue
                    book = probe.get("book") or {}
                    normalized = _polymarket_book_for_selected_outcome(book)
                    market = _short_crypto_market_from_polymarket_series(
                        row,
                        normalized,
                        asset=asset,
                        window_minutes=int(window),
                        direction=direction,
                        token_id=str(probe.get("token_id_used") or token_id),
                    )
                    if simulate_fill(build_intent(market, None), build_intent(market, None)["book"])["filled"]:
                        markets.append(market)
                        _record_market_log(counters, row, timing, accepted=True)
                        _record_accepted_lead_time(counters, timing["lead_time_minutes"])
                    else:
                        _count_rejection(counters, "no_executable_book")
    return markets


def _discover_polymarket_gamma_fallback(config: PaperConfig, counters: dict[str, Any]) -> list[ShortCryptoMarket]:
    rows: list[dict[str, Any]] = []
    client = PolymarketClient(raw_dir="data/raw")
    for asset in config.assets:
        rows.extend(client.get_active_markets(keyword=asset, category=None, sport=None, limit=250))
    counters["polymarket_markets_seen"] += len(rows)
    out: list[ShortCryptoMarket] = []
    for row in rows:
        slug = str(row.get("slug") or "")
        title = str(row.get("title") or row.get("question") or slug).lower()
        if not any(asset.lower() in title or asset.lower() in slug for asset in config.assets):
            continue
        if not any(f"{window}m" in slug or f"{window} min" in title or f"{window} minute" in title for window in config.windows):
            continue
        normalized = normalize_short_crypto_markets([], [_attach_polymarket_book(row)])
        out.extend(normalized)
    return out


def _short_crypto_market_from_polymarket_series(
    row: dict[str, Any],
    book: dict[str, Any],
    *,
    asset: str,
    window_minutes: int,
    direction: str,
    token_id: str,
) -> ShortCryptoMarket:
    yes_bid = _best_bid(_levels(book, "yes", "bids"))
    yes_ask = _best_ask(_levels(book, "yes", "asks"))
    liquidity = _book_depth(book.get("yes") or {}, "bids") + _book_depth(book.get("yes") or {}, "asks")
    raw = dict(row)
    raw["token_id"] = token_id
    raw["selected_outcome_direction"] = direction
    raw["clob_orderbook"] = book
    start_ts = _market_start_ts(row)
    return ShortCryptoMarket(
        asset=asset.upper(),
        venue="polymarket",
        ticker=str(row.get("conditionId") or row.get("condition_id") or row.get("id") or row.get("slug") or ""),
        start_ts=start_ts,
        end_ts=_ts(row.get("endDate") or row.get("end_date") or row.get("endDateIso")),
        direction=direction,
        yes_bid=yes_bid["price"] if yes_bid else None,
        yes_ask=yes_ask["price"] if yes_ask else None,
        no_bid=None,
        no_ask=None,
        liquidity=liquidity,
        window_minutes=window_minutes,
        timestamp=time.time(),
        raw=raw,
    )


def _looks_like_enabled_kalshi_short_crypto(row: dict[str, Any], assets: list[str], windows: list[int]) -> bool:
    ticker = str(row.get("ticker") or row.get("market_identity", {}).get("ticker") or "").upper()
    title = str(row.get("title") or row.get("question") or row.get("market_identity", {}).get("title") or "").upper()
    text = f"{ticker} {title}"
    if not any(asset == "BTC" and ("KXBT" in ticker or "BITCOIN" in text or " BTC " in f" {text} ") or asset in text for asset in assets):
        return False
    if not any(marker in ticker for marker in ("-B", "-T")) and "UP" not in text and "DOWN" not in text:
        return False
    parsed_window = _rough_window_minutes(row)
    return parsed_window is None or parsed_window in windows


def _rough_window_minutes(row: dict[str, Any]) -> int | None:
    for key in ("window_minutes", "window", "duration_minutes"):
        value = row.get(key)
        if value is not None:
            try:
                return int(value)
            except Exception:
                pass
    text = str(row.get("ticker") or "") + " " + str(row.get("title") or row.get("question") or "")
    text = text.upper()
    if "5M" in text or "5 MIN" in text:
        return 5
    if "10M" in text or "10 MIN" in text:
        return 10
    if "15M" in text or "15 MIN" in text:
        return 15
    return None


def build_intent(market: ShortCryptoMarket, spot_price: float | None, *, now_ts: float | None = None) -> dict[str, Any]:
    now_ts = now_ts if now_ts is not None else time.time()
    side = "YES" if market.venue == "polymarket" and market.raw.get("token_id") else "YES" if market.direction == "down" else "NO"
    implied = market.implied_prob("yes" if side == "YES" else "no")
    if implied is None and side == "NO" and market.yes_bid is not None and market.yes_ask is not None:
        implied = 1.0 - ((market.yes_bid + market.yes_ask) / 2.0)
    implied = float(implied if implied is not None else 0.0)
    model = min(0.99, max(0.01, implied + 0.05))
    entry_time = _iso(now_ts)
    end_ts = float(market.end_ts or (now_ts + (market.window_minutes or 5) * 60))
    raw = dict(market.raw or {})
    return {
        "venue": market.venue,
        "asset": market.asset,
        "market": raw.get("condition_id") or raw.get("market_id") or market.ticker,
        "ticker": market.ticker,
        "slug": raw.get("slug"),
        "token_id": _selected_token_id(raw, side),
        "direction": market.direction,
        "side": side,
        "action": "BUY",
        "window_minutes": int(market.window_minutes or 5),
        "entry_time": entry_time,
        "end_time": _iso(end_ts),
        "spot_price": spot_price,
        "model_probability": model,
        "implied_probability": implied,
        "edge": model - implied,
        "size": DEFAULT_SIZE,
        "book": _normalized_book_from_market(market),
        "liquidity": market.liquidity,
        "book_timestamp": market.timestamp,
        "market_start_time": _iso(market.start_ts) if market.start_ts is not None else None,
        "lead_time_minutes": ((float(market.start_ts) - now_ts) / 60.0) if market.start_ts is not None else None,
        "raw_market": raw,
    }


def validate_intent(intent: dict[str, Any], config: PaperConfig, store: ShortCryptoPaperStore, exposure: float) -> str | None:
    if intent["venue"] not in config.venues:
        return "venue_not_enabled"
    if intent["asset"] not in config.assets:
        return "asset_not_enabled"
    if int(intent["window_minutes"]) not in config.windows:
        return "window_not_enabled"
    if _parse_ts(intent["end_time"]) <= _parse_ts(intent["entry_time"]):
        return "end_time_not_after_entry"
    if float(intent["edge"]) < config.min_edge:
        return "edge_below_threshold"
    if float(intent.get("liquidity") or 0.0) < config.min_liquidity:
        return "liquidity_below_threshold"
    ts = intent.get("book_timestamp")
    if ts is None or float(ts) > time.time() + 5.0 or time.time() - float(ts) > config.freshness_seconds:
        return "stale_order_book"
    if store.has_duplicate_open(intent):
        return "duplicate_market"
    estimated = simulate_fill(intent, intent["book"])
    if not estimated["filled"]:
        return estimated["reason"]
    if exposure + float(estimated["paper_cost"]) > config.max_paper_exposure:
        return "max_paper_exposure_breached"
    return None


def _debug_counters() -> dict[str, Any]:
    return {
        "kalshi_markets_seen": 0,
        "polymarket_markets_seen": 0,
        "markets_rejected_by_reason": {},
        "executable_books_seen": 0,
        "duplicate_blocks": 0,
        "exposure_blocks": 0,
        "edge_blocks": 0,
        "liquidity_blocks": 0,
        "freshness_blocks": 0,
        "future_market_blocks": 0,
        "average_market_lead_time_minutes": None,
        "accepted_market_logs": [],
        "rejected_future_market_logs": [],
        "_accepted_market_lead_time_sum": 0.0,
        "_accepted_market_lead_time_count": 0,
    }


def _count_rejection(counters: dict[str, Any], reason: str | None) -> None:
    reason = reason or "unknown"
    rejected = counters.setdefault("markets_rejected_by_reason", {})
    rejected[reason] = int(rejected.get(reason, 0)) + 1
    if reason == "duplicate_market":
        counters["duplicate_blocks"] = int(counters.get("duplicate_blocks", 0)) + 1
    elif reason == "max_paper_exposure_breached":
        counters["exposure_blocks"] = int(counters.get("exposure_blocks", 0)) + 1
    elif reason == "edge_below_threshold":
        counters["edge_blocks"] = int(counters.get("edge_blocks", 0)) + 1
    elif reason == "liquidity_below_threshold":
        counters["liquidity_blocks"] = int(counters.get("liquidity_blocks", 0)) + 1
    elif reason == "stale_order_book":
        counters["freshness_blocks"] = int(counters.get("freshness_blocks", 0)) + 1
    elif reason == "future_market":
        counters["future_market_blocks"] = int(counters.get("future_market_blocks", 0)) + 1


def market_lead_time(row: dict[str, Any], *, now_ts: float | None = None, max_lead_time_minutes: float = DEFAULT_MAX_MARKET_LEAD_TIME_MINUTES) -> dict[str, Any]:
    now_ts = now_ts if now_ts is not None else time.time()
    start_ts = _market_start_ts(row)
    end_ts = _ts(row.get("endDate") or row.get("end_date") or row.get("endDateIso"))
    lead_time = None if start_ts is None else (start_ts - now_ts) / 60.0
    reason = None
    if end_ts is not None and end_ts <= now_ts:
        reason = "market_expired"
    elif start_ts is not None and start_ts < now_ts:
        reason = "market_already_started"
    elif lead_time is not None and lead_time > float(max_lead_time_minutes):
        reason = "future_market"
    return {
        "market_start_time": _iso(start_ts) if start_ts is not None else None,
        "market_end_time": _iso(end_ts) if end_ts is not None else None,
        "lead_time_minutes": lead_time,
        "reason": reason,
    }


def _market_start_ts(row: dict[str, Any]) -> float | None:
    for key in ("startTime", "start_time", "eventStartTime", "event_start_time", "startDate", "start_date"):
        ts = _ts(row.get(key))
        if ts is not None:
            return ts
    return None


def _record_accepted_lead_time(counters: dict[str, Any], lead_time: float | None) -> None:
    if lead_time is None:
        return
    counters["_accepted_market_lead_time_sum"] = float(counters.get("_accepted_market_lead_time_sum") or 0.0) + float(lead_time)
    counters["_accepted_market_lead_time_count"] = int(counters.get("_accepted_market_lead_time_count") or 0) + 1


def _finalize_lead_time_stats(counters: dict[str, Any]) -> None:
    count = int(counters.get("_accepted_market_lead_time_count") or 0)
    if count:
        counters["average_market_lead_time_minutes"] = float(counters.get("_accepted_market_lead_time_sum") or 0.0) / count
    counters.pop("_accepted_market_lead_time_sum", None)
    counters.pop("_accepted_market_lead_time_count", None)


def _record_market_log(counters: dict[str, Any], row: dict[str, Any], timing: dict[str, Any], *, accepted: bool) -> None:
    item = {
        "market_title": row.get("question") or row.get("title"),
        "market_slug": row.get("slug") or row.get("marketSlug") or row.get("id"),
        "market_start_time": timing.get("market_start_time"),
        "market_end_time": timing.get("market_end_time"),
        "lead_time_minutes": timing.get("lead_time_minutes"),
    }
    if accepted:
        counters.setdefault("accepted_market_logs", []).append(item)
        return
    item["reason"] = timing.get("reason")
    counters.setdefault("rejected_future_market_logs", []).append(item)


def settle_due(db_path: str = DEFAULT_DB_PATH, *, now_ts: float | None = None) -> dict[str, Any]:
    store = ShortCryptoPaperStore(db_path)
    due = store.due_open_trades(now_ts=now_ts)
    settled = 0
    unknown = 0
    for trade in due:
        settlement = settle_trade(trade, now_ts=now_ts)
        store.mark_settled(trade, settlement)
        if settlement["result"] == "unknown":
            unknown += 1
        else:
            settled += 1
    return {"due_trades": len(due), "settled": settled, "unknown": unknown}


def settle_trade(trade: dict[str, Any], *, now_ts: float | None = None) -> dict[str, Any]:
    now_ts = now_ts if now_ts is not None else time.time()
    if now_ts < _parse_ts(trade["end_time"]):
        return {"result": "unknown", "payout": None, "pnl": None, "roi": None, "settlement_source": "not_past_end_time"}
    raw = _load_json(trade.get("raw_json"))
    market_raw = raw.get("raw_market") if isinstance(raw, dict) else {}
    result = _explicit_result(market_raw, trade)
    source = "market_result"
    if result is None:
        result = _reference_price_result(market_raw, trade)
        source = "clearly_labeled_reference_price" if result is not None else "unavailable"
    if result is None:
        return {"result": "unknown", "payout": None, "pnl": None, "roi": None, "settlement_source": source}
    won = result == "won"
    payout = float(trade["size"]) if won else 0.0
    pnl = payout - float(trade["paper_cost"]) - float(trade.get("fee") or 0.0)
    roi = pnl / float(trade["paper_cost"]) if float(trade["paper_cost"]) else None
    return {"result": result, "payout": payout, "pnl": pnl, "roi": roi, "settlement_source": source}


def performance_report(db_path: str = DEFAULT_DB_PATH) -> dict[str, Any]:
    store = ShortCryptoPaperStore(db_path)
    with store.connect() as conn:
        trades = [dict(row) for row in conn.execute("SELECT * FROM paper_trades").fetchall()]
        settlements = [dict(row) for row in conn.execute("SELECT * FROM paper_settlements").fetchall()]
    settlement_by_trade = {int(s["trade_id"]): s for s in settlements}
    closed = [t for t in trades if int(t["id"]) in settlement_by_trade and settlement_by_trade[int(t["id"])]["result"] in {"won", "lost"}]
    open_trades = [t for t in trades if t["status"] == "open"]
    wins = sum(1 for t in closed if settlement_by_trade[int(t["id"])]["result"] == "won")
    pnl = sum(float(settlement_by_trade[int(t["id"])]["pnl"] or 0.0) for t in closed)
    cost = sum(float(t["paper_cost"] or 0.0) for t in closed)
    report = {
        "total_trades": len(trades),
        "closed_trades": len(closed),
        "open_trades": len(open_trades),
        "win_rate": wins / len(closed) if closed else None,
        "total_paper_pnl": pnl,
        "roi": pnl / cost if cost else None,
        "average_edge": _avg([t["edge"] for t in trades]),
        "average_entry_price": _avg([t["entry_price"] for t in trades]),
        "by_venue": _group_report(trades, settlement_by_trade, "venue"),
        "by_asset": _group_report(trades, settlement_by_trade, "asset"),
        "by_window": _group_report(trades, settlement_by_trade, "window_minutes"),
        "calibration_buckets": _calibration_buckets(trades, settlement_by_trade),
        "warnings": [],
    }
    if len(closed) < 100:
        report["warnings"].append("sample size < 100 closed trades")
    return report


def fetch_spot_prices(assets: list[str]) -> dict[str, float]:
    prices: dict[str, float] = {}
    for asset in assets:
        symbol = f"{asset.upper()}-USD"
        try:
            req = Request(f"https://api.exchange.coinbase.com/products/{symbol}/ticker", headers={"User-Agent": "polylens/0.1"})
            with urlopen(req, timeout=5) as response:
                payload = json.loads(response.read().decode("utf-8"))
            prices[asset.upper()] = float(payload["price"])
        except Exception:
            continue
    return prices


def _attach_kalshi_book(row: dict[str, Any]) -> dict[str, Any]:
    book = row.get("orderbook") or {}
    yes = _kalshi_levels(book, "yes")
    no = _kalshi_levels(book, "no")
    yes_bid = _best_bid(yes)
    no_bid = _best_bid(no)
    if yes_bid:
        row["yes_bid"] = yes_bid["price"]
    if no_bid:
        row["no_bid"] = no_bid["price"]
    if no_bid:
        row["yes_ask"] = round(1.0 - no_bid["price"], 6)
    if yes_bid:
        row["no_ask"] = round(1.0 - yes_bid["price"], 6)
    row["book_timestamp"] = time.time()
    return row


def _attach_polymarket_book(row: dict[str, Any]) -> dict[str, Any]:
    token_ids = _parse_list(row.get("token_ids") or row.get("clobTokenIds"))
    outcomes = [str(x).lower() for x in _parse_list(row.get("outcomes"))]
    yes_idx = next((i for i, item in enumerate(outcomes) if item in {"yes", "up", "higher"}), 0)
    no_idx = next((i for i, item in enumerate(outcomes) if item in {"no", "down", "lower"}), 1 if len(token_ids) > 1 else 0)
    book: dict[str, Any] = {"yes": {}, "no": {}, "timestamp": time.time()}
    for label, idx in (("yes", yes_idx), ("no", no_idx)):
        if idx >= len(token_ids):
            continue
        token_id = str(token_ids[idx])
        try:
            payload = _get_json("https://clob.polymarket.com/book", {"token_id": token_id})
            book[label] = {"bids": payload.get("bids") or [], "asks": payload.get("asks") or []}
        except Exception as exc:
            row[f"{label}_book_error"] = str(exc)
    row["clob_orderbook"] = book
    return row


def _polymarket_book_for_selected_outcome(book: dict[str, Any]) -> dict[str, Any]:
    return {
        "yes": {
            "bids": book.get("bids") or [],
            "asks": book.get("asks") or [],
        },
        "no": {"bids": [], "asks": []},
    }


def _polymarket_outcome_direction(outcome: Any) -> str | None:
    text = str(outcome or "").strip().lower()
    if text in {"down", "no", "lower", "below"}:
        return "down"
    if text in {"up", "yes", "higher", "above"}:
        return "up"
    return None


def _normalized_book_from_market(market: ShortCryptoMarket) -> dict[str, Any]:
    return {
        "yes": {
            "bids": [{"price": market.yes_bid, "size": market.liquidity, "source": "yes_bid"}] if market.yes_bid is not None else [],
            "asks": [{"price": market.yes_ask, "size": market.liquidity, "source": "yes_ask"}] if market.yes_ask is not None else [],
        },
        "no": {
            "bids": [{"price": market.no_bid, "size": market.liquidity, "source": "no_bid"}] if market.no_bid is not None else [],
            "asks": [{"price": market.no_ask, "size": market.liquidity, "source": "no_ask"}] if market.no_ask is not None else [],
        },
    }


def _levels(book: dict[str, Any], side: str, kind: str) -> list[Any]:
    container = book.get(side) or book.get(side.upper()) or {}
    if isinstance(container, dict):
        return container.get(kind) or []
    return []


def _book_depth(container: dict[str, Any], side: str) -> float:
    total = 0.0
    for level in container.get(side) or []:
        try:
            if isinstance(level, dict):
                total += float(level.get("size") or level.get("quantity") or level.get("count") or 0.0)
            elif isinstance(level, (list, tuple)) and len(level) > 1:
                total += float(level[1])
        except Exception:
            continue
    return total


def _best_bid(levels: list[Any]) -> dict[str, Any] | None:
    return _best_level(levels, reverse=True)


def _best_ask(levels: list[Any]) -> dict[str, Any] | None:
    return _best_level(levels, reverse=False)


def _best_level(levels: list[Any], *, reverse: bool) -> dict[str, Any] | None:
    parsed = []
    for level in levels or []:
        price = size = None
        source = None
        if isinstance(level, dict):
            price = level.get("price") or level.get("p")
            size = level.get("size") or level.get("quantity") or level.get("count") or level.get("q")
            source = level.get("source")
        elif isinstance(level, (list, tuple)) and level:
            price = level[0]
            size = level[1] if len(level) > 1 else None
        try:
            price_f = float(price)
            size_f = float(size if size is not None else 1.0)
        except Exception:
            continue
        if price_f > 1.0:
            price_f = price_f / 100.0
        if 0.0 < price_f < 1.0 and size_f > 0:
            parsed.append({"price": price_f, "size": size_f, "source": source})
    if not parsed:
        return None
    return sorted(parsed, key=lambda item: item["price"], reverse=reverse)[0]


def _kalshi_levels(orderbook: dict[str, Any], side: str) -> list[Any]:
    ladder = orderbook.get("orderbook") or orderbook.get("orderbook_fp") or orderbook
    if not isinstance(ladder, dict):
        return []
    return ladder.get(f"{side}_dollars") or ladder.get(side) or []


def _selected_token_id(raw: dict[str, Any], side: str) -> str | None:
    token_ids = _parse_list(raw.get("token_ids") or raw.get("clobTokenIds"))
    if not token_ids:
        return None
    outcomes = [str(x).lower() for x in _parse_list(raw.get("outcomes"))]
    wanted = {"YES": {"yes", "up", "higher"}, "NO": {"no", "down", "lower"}}[side.upper()]
    for idx, outcome in enumerate(outcomes):
        if outcome in wanted and idx < len(token_ids):
            return str(token_ids[idx])
    return str(token_ids[0 if side.upper() == "YES" else min(1, len(token_ids) - 1)])


def _explicit_result(raw: dict[str, Any], trade: dict[str, Any]) -> str | None:
    for key in ("winning_outcome", "winner", "resolved_outcome", "result"):
        value = raw.get(key) if isinstance(raw, dict) else None
        if value:
            text = str(value).lower()
            side = str(trade["side"]).lower()
            if side in text:
                return "won"
            if ("yes" in text or "no" in text or "up" in text or "down" in text):
                return "lost"
    return None


def _reference_price_result(raw: dict[str, Any], trade: dict[str, Any]) -> str | None:
    if not isinstance(raw, dict):
        return None
    source = str(raw.get("settlement_source") or raw.get("reference_price_source") or "").lower()
    if "reference" not in source and "settlement" not in source:
        return None
    ref = _float_or_none(raw.get("final_reference_price") or raw.get("settlement_reference_price"))
    strike = _float_or_none(raw.get("strike_price") or raw.get("strike"))
    if ref is None or strike is None:
        return None
    direction = str(trade["direction"]).lower()
    side = str(trade["side"]).upper()
    yes_won = ref > strike if direction == "up" else ref <= strike
    won = yes_won if side == "YES" else not yes_won
    return "won" if won else "lost"


def _group_report(trades: list[dict[str, Any]], settlements: dict[int, dict[str, Any]], key: str) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for trade in trades:
        label = str(trade[key])
        item = out.setdefault(label, {"total_trades": 0, "closed_trades": 0, "win_rate": None, "pnl": 0.0, "roi": None})
        item["total_trades"] += 1
        settlement = settlements.get(int(trade["id"]))
        if settlement and settlement["result"] in {"won", "lost"}:
            item["closed_trades"] += 1
            item["pnl"] += float(settlement["pnl"] or 0.0)
    for label, item in out.items():
        closed = [t for t in trades if str(t[key]) == label and int(t["id"]) in settlements and settlements[int(t["id"])]["result"] in {"won", "lost"}]
        wins = sum(1 for t in closed if settlements[int(t["id"])]["result"] == "won")
        cost = sum(float(t["paper_cost"] or 0.0) for t in closed)
        item["win_rate"] = wins / len(closed) if closed else None
        item["roi"] = item["pnl"] / cost if cost else None
    return out


def _calibration_buckets(trades: list[dict[str, Any]], settlements: dict[int, dict[str, Any]]) -> dict[str, Any]:
    buckets = {f"{i}-{i+10}%": {"count": 0, "wins": 0, "observed_win_rate": None, "average_model_probability": None} for i in range(0, 100, 10)}
    sums = {key: 0.0 for key in buckets}
    for trade in trades:
        prob = max(0.0, min(0.999999, float(trade["model_probability"] or 0.0)))
        lower = int(math.floor(prob * 10.0) * 10)
        label = f"{lower}-{lower+10}%"
        buckets[label]["count"] += 1
        sums[label] += prob
        settlement = settlements.get(int(trade["id"]))
        if settlement and settlement["result"] == "won":
            buckets[label]["wins"] += 1
    for label, item in buckets.items():
        item["observed_win_rate"] = item["wins"] / item["count"] if item["count"] else None
        item["average_model_probability"] = sums[label] / item["count"] if item["count"] else None
    return buckets


def _get_json(url: str, params: dict[str, Any]) -> dict[str, Any]:
    query = urlencode(params)
    request = Request(f"{url}?{query}" if query else url, headers={"User-Agent": "polylens/0.1", "Accept": "application/json"})
    with urlopen(request, timeout=10) as response:
        payload = json.loads(response.read().decode("utf-8"))
    return payload if isinstance(payload, dict) else {}


def _parse_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else []
        except Exception:
            return [item.strip() for item in value.split(",") if item.strip()]
    return []


def _avg(values: list[Any]) -> float | None:
    nums = [float(v) for v in values if v is not None]
    return sum(nums) / len(nums) if nums else None


def _float_or_none(value: Any) -> float | None:
    try:
        return None if value is None or value == "" else float(value)
    except Exception:
        return None


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _load_json(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(value or "{}")
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def _parse_ts(value: Any) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    return datetime.fromisoformat(text).timestamp()


def _ts(value: Any) -> float | None:
    if value in {None, ""}:
        return None
    try:
        return _parse_ts(value)
    except Exception:
        return None


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(float(ts), timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _now() -> str:
    return _iso(time.time())


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, default=str)
