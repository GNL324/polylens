from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Callable
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from src.analysis.short_crypto_markets import ShortCryptoMarket
from src.analysis.short_crypto_paper import (
    DEFAULT_DB_PATH,
    PaperConfig,
    ShortCryptoPaperStore,
    discover_markets,
    simulate_fill,
)

STRATEGY_NAME = "btc_5m_momentum_close"
MARKET_FAMILY = "BTC 5-minute UP/DOWN"


@dataclass(frozen=True)
class Btc5mMomentumConfig:
    paper: bool = True
    live: bool = False
    minutes: int = 60
    db_path: str = DEFAULT_DB_PATH
    entry_window_sec: float = 120.0
    exit_before_sec: float = 15.0
    max_spread: float = 0.08
    stale_quote_sec: float = 10.0
    min_liquidity: float = 5.0
    daily_loss_cap: float | None = 10.0
    fixed_paper_size: float = 1.0
    min_side_price: float = 0.60
    min_impulse_abs: float = 25.0
    min_impulse_pct: float = 0.0015
    max_trades_per_cycle: int = 1


@dataclass(frozen=True)
class BtcSpotSnapshot:
    bucket_open: float
    current: float
    timestamp: float
    bucket_start_ts: float | None = None
    source: str = "unknown"


def reject_live_mode(config: Btc5mMomentumConfig) -> None:
    if config.live or not config.paper:
        raise ValueError(f"{STRATEGY_NAME} is paper-only; live execution is not implemented")


def strategy_profile(config: Btc5mMomentumConfig | None = None) -> dict[str, Any]:
    cfg = config or Btc5mMomentumConfig()
    payload = asdict(cfg)
    payload.update(
        {
            "name": STRATEGY_NAME,
            "market_family": MARKET_FAMILY,
            "asset": "BTC",
            "window_minutes": 5,
            "paper_only": True,
            "fixed_paper_sizing_only": True,
            "stronger_side_selection": True,
            "daily_loss_cap_hook": cfg.daily_loss_cap is not None,
        }
    )
    return payload


def run_btc_5m_momentum(
    config: Btc5mMomentumConfig | None = None,
    *,
    market_provider: Callable[[Btc5mMomentumConfig], list[ShortCryptoMarket]] | None = None,
    spot_provider: Callable[[], BtcSpotSnapshot | None] | None = None,
    now_ts: float | None = None,
    current_daily_pnl: float = 0.0,
) -> dict[str, Any]:
    config = config or Btc5mMomentumConfig()
    reject_live_mode(config)
    now_ts = now_ts if now_ts is not None else time.time()
    store = ShortCryptoPaperStore(config.db_path)
    paper_config = _paper_config(config)
    run_id = store.start_run(paper_config)
    result: dict[str, Any] = {
        "strategy": STRATEGY_NAME,
        "profile": strategy_profile(config),
        "paper_only": True,
        "run_id": run_id,
        "markets_seen": 0,
        "pairs_seen": 0,
        "paper_trades_created": 0,
        "rejected_signals": 0,
        "skip_reasons": {},
    }
    if daily_loss_cap_blocked(config, current_daily_pnl=current_daily_pnl):
        result["skip_reasons"] = {"daily_loss_cap": 1}
        store.finish_run(run_id, created_trades=0, rejected_signals=0, meta=result)
        return result

    provider = market_provider or _default_market_provider
    spot_fn = spot_provider or fetch_btc_spot_snapshot
    try:
        markets = provider(config)
    except Exception as exc:
        result["errors"] = [{"source": "markets", "error": str(exc)}]
        store.finish_run(run_id, created_trades=0, rejected_signals=0, meta=result)
        return result
    result["markets_seen"] = len(markets)
    spot = spot_fn()
    groups = group_up_down_markets(markets)
    result["pairs_seen"] = len(groups)
    created = 0
    rejected = 0
    for pair in groups:
        decision = evaluate_momentum_pair(pair, spot, config=config, now_ts=now_ts)
        intent = decision.get("intent")
        if not decision["trade"]:
            reason = str(decision["skip_reason"])
            result["skip_reasons"][reason] = int(result["skip_reasons"].get(reason, 0)) + 1
            rejected += 1
            if intent is not None:
                store.save_signal(intent, run_id=run_id, status="rejected", reason=reason)
            continue
        if intent is None:
            result["skip_reasons"]["missing_intent"] = int(result["skip_reasons"].get("missing_intent", 0)) + 1
            rejected += 1
            continue
        if store.has_duplicate_open(intent):
            intent["skip_reason"] = "duplicate_market"
            store.save_signal(intent, run_id=run_id, status="rejected", reason="duplicate_market")
            result["skip_reasons"]["duplicate_market"] = int(result["skip_reasons"].get("duplicate_market", 0)) + 1
            rejected += 1
            continue
        fill = simulate_fill(intent, intent["book"])
        if not fill["filled"]:
            reason = str(fill.get("reason") or "no_fill")
            intent["skip_reason"] = reason
            store.save_signal(intent, run_id=run_id, status="rejected", reason=reason)
            result["skip_reasons"][reason] = int(result["skip_reasons"].get(reason, 0)) + 1
            rejected += 1
            continue
        intent.update(fill)
        intent["paper_cost"] = float(intent["entry_price"]) * float(intent["size"])
        intent["expected_value"] = 0.0
        intent["strategy_label"] = STRATEGY_NAME
        signal_id = store.save_signal(intent, run_id=run_id, status="accepted")
        store.save_trade(intent, signal_id=signal_id, run_id=run_id)
        created += 1
        if created >= config.max_trades_per_cycle:
            break
    result["paper_trades_created"] = created
    result["rejected_signals"] = rejected
    store.finish_run(run_id, created_trades=created, rejected_signals=rejected, meta=result)
    return result


def evaluate_momentum_pair(
    pair: dict[str, ShortCryptoMarket],
    spot: BtcSpotSnapshot | None,
    *,
    config: Btc5mMomentumConfig | None = None,
    now_ts: float | None = None,
) -> dict[str, Any]:
    config = config or Btc5mMomentumConfig()
    now_ts = now_ts if now_ts is not None else time.time()
    up = pair.get("up")
    down = pair.get("down")
    if up is None or down is None:
        return {"trade": False, "skip_reason": "missing_up_down_pair", "intent": None}
    seconds_to_expiry = min(_seconds_to_expiry(up, now_ts), _seconds_to_expiry(down, now_ts))
    base_metadata = _base_metadata(seconds_to_expiry=seconds_to_expiry, config=config)
    if seconds_to_expiry <= 0:
        return {"trade": False, "skip_reason": "market_expired", "intent": _rejected_intent(up, base_metadata, "market_expired", now_ts)}
    if seconds_to_expiry > config.entry_window_sec:
        return {"trade": False, "skip_reason": "outside_entry_window", "intent": _rejected_intent(up, base_metadata, "outside_entry_window", now_ts)}
    if seconds_to_expiry <= config.exit_before_sec:
        return {"trade": False, "skip_reason": "inside_exit_window", "intent": _rejected_intent(up, base_metadata, "inside_exit_window", now_ts)}
    quote_reason = _quote_guard(up, config, now_ts) or _quote_guard(down, config, now_ts)
    if quote_reason:
        return {"trade": False, "skip_reason": quote_reason, "intent": _rejected_intent(up, base_metadata, quote_reason, now_ts)}
    selected, selected_side, opposite = select_stronger_side(up, down, min_side_price=config.min_side_price)
    if selected is None or opposite is None or selected_side is None:
        reason = "side_threshold_not_crossed"
        return {"trade": False, "skip_reason": reason, "intent": _rejected_intent(up, base_metadata, reason, now_ts)}
    impulse = impulse_confirmation(spot, selected_side=selected_side, config=config, now_ts=now_ts)
    metadata = {
        **base_metadata,
        **impulse,
        "selected_side": selected_side,
        "opposite_side_price": _ask(opposite),
        "spread": _spread(selected),
        "liquidity": _liquidity(selected),
    }
    if not impulse["impulse_confirmed"]:
        reason = str(impulse["skip_reason"])
        return {"trade": False, "skip_reason": reason, "intent": _rejected_intent(selected, metadata, reason, now_ts)}
    intent = build_paper_intent(selected, selected_side=selected_side, metadata=metadata, config=config, now_ts=now_ts)
    return {"trade": True, "skip_reason": None, "intent": intent, "metadata": metadata}


def select_stronger_side(
    up: ShortCryptoMarket,
    down: ShortCryptoMarket,
    *,
    min_side_price: float,
) -> tuple[ShortCryptoMarket | None, str | None, ShortCryptoMarket | None]:
    candidates = [
        ("up", up, _ask(up)),
        ("down", down, _ask(down)),
    ]
    crossed = [(side, market, price) for side, market, price in candidates if price is not None and price >= min_side_price]
    if not crossed:
        return None, None, None
    crossed.sort(key=lambda item: (float(item[2]), 1 if item[0] == "up" else 0), reverse=True)
    selected_side, selected_market, _price = crossed[0]
    opposite_market = down if selected_side == "up" else up
    return selected_market, selected_side, opposite_market


def impulse_confirmation(
    spot: BtcSpotSnapshot | None,
    *,
    selected_side: str,
    config: Btc5mMomentumConfig,
    now_ts: float | None = None,
) -> dict[str, Any]:
    now_ts = now_ts if now_ts is not None else time.time()
    if spot is None:
        return _impulse_payload(None, None, None, None, False, "spot_unavailable")
    if now_ts - float(spot.timestamp) > config.stale_quote_sec:
        return _impulse_payload(spot.bucket_open, spot.current, None, None, False, "spot_stale")
    impulse_abs = float(spot.current) - float(spot.bucket_open)
    impulse_pct = impulse_abs / float(spot.bucket_open) if spot.bucket_open else 0.0
    directional_abs = impulse_abs if selected_side == "up" else -impulse_abs
    directional_pct = impulse_pct if selected_side == "up" else -impulse_pct
    confirmed = directional_abs >= config.min_impulse_abs and directional_pct >= config.min_impulse_pct
    return _impulse_payload(
        spot.bucket_open,
        spot.current,
        abs(impulse_abs),
        abs(impulse_pct),
        confirmed,
        None if confirmed else "impulse_not_confirmed",
    )


def build_paper_intent(
    market: ShortCryptoMarket,
    *,
    selected_side: str,
    metadata: dict[str, Any],
    config: Btc5mMomentumConfig,
    now_ts: float,
) -> dict[str, Any]:
    entry_price = float(_ask(market) or 0.0)
    end_ts = float(market.end_ts or (now_ts + 300.0))
    raw_market = dict(market.raw or {})
    raw = {
        **metadata,
        "strategy": STRATEGY_NAME,
        "strategy_label": STRATEGY_NAME,
        "market_family": MARKET_FAMILY,
        "raw_market": raw_market,
    }
    return {
        "venue": "polymarket",
        "asset": "BTC",
        "market": raw_market.get("condition_id") or raw_market.get("market_id") or market.ticker,
        "ticker": market.ticker,
        "slug": raw_market.get("slug"),
        "token_id": raw_market.get("token_id"),
        "direction": selected_side,
        "side": "YES",
        "action": "BUY",
        "window_minutes": 5,
        "entry_time": _iso(now_ts),
        "end_time": _iso(end_ts),
        "spot_price": metadata.get("btc_current"),
        "model_probability": entry_price,
        "implied_probability": entry_price,
        "edge": 0.0,
        "size": config.fixed_paper_size,
        "book": _book(market),
        "liquidity": metadata.get("liquidity"),
        "book_timestamp": market.timestamp,
        "market_start_time": _iso(market.start_ts) if market.start_ts is not None else None,
        "lead_time_minutes": ((float(market.start_ts) - now_ts) / 60.0) if market.start_ts is not None else None,
        "entry_price": entry_price,
        "paper_cost": entry_price * config.fixed_paper_size,
        "fee": 0.0,
        "expected_value": 0.0,
        "strategy_label": STRATEGY_NAME,
        "skip_reason": metadata.get("skip_reason"),
        **metadata,
        "raw_market": raw_market,
        "raw_json_metadata": raw,
    }


def group_up_down_markets(markets: list[ShortCryptoMarket]) -> list[dict[str, ShortCryptoMarket]]:
    grouped: dict[str, dict[str, ShortCryptoMarket]] = {}
    for market in markets:
        if market.asset.upper() != "BTC" or int(market.window_minutes or 0) != 5:
            continue
        direction = str(market.direction or "").lower()
        if direction not in {"up", "down"}:
            continue
        raw = market.raw or {}
        key = str(raw.get("slug") or market.ticker or raw.get("condition_id") or "")
        if key.endswith(f"-{direction}"):
            key = key[: -(len(direction) + 1)]
        grouped.setdefault(key, {})[direction] = market
    return [item for item in grouped.values() if "up" in item and "down" in item]


def daily_loss_cap_blocked(config: Btc5mMomentumConfig, *, current_daily_pnl: float) -> bool:
    if config.daily_loss_cap is None:
        return False
    return float(current_daily_pnl) <= -abs(float(config.daily_loss_cap))


def fetch_btc_spot_snapshot() -> BtcSpotSnapshot | None:
    now_ts = time.time()
    bucket_start = int(now_ts // 300) * 300
    params = urlencode({"granularity": 300, "start": _iso(bucket_start - 300), "end": _iso(now_ts)})
    url = f"https://api.exchange.coinbase.com/products/BTC-USD/candles?{params}"
    try:
        request = Request(url, headers={"User-Agent": "polylens-paper-btc-5m-momentum"})
        with urlopen(request, timeout=5) as response:
            candles = json.loads(response.read().decode("utf-8"))
        if not isinstance(candles, list) or not candles:
            return None
        current_bucket = max(candles, key=lambda row: int(row[0]))
        return BtcSpotSnapshot(
            bucket_open=float(current_bucket[3]),
            current=float(current_bucket[4]),
            timestamp=now_ts,
            bucket_start_ts=float(current_bucket[0]),
            source="coinbase_candles",
        )
    except Exception:
        return None


def _default_market_provider(config: Btc5mMomentumConfig) -> list[ShortCryptoMarket]:
    errors: list[dict[str, Any]] = []
    counters: dict[str, Any] = {}
    return discover_markets(_paper_config(config), errors, counters)


def _paper_config(config: Btc5mMomentumConfig) -> PaperConfig:
    return PaperConfig(
        venues=["polymarket"],
        assets=["BTC"],
        windows=[5],
        max_trades=config.max_trades_per_cycle,
        max_paper_exposure=10_000.0,
        min_edge=-1.0,
        min_liquidity=config.min_liquidity,
        freshness_seconds=config.stale_quote_sec,
        db_path=config.db_path,
        strategy_label=STRATEGY_NAME,
    )


def _quote_guard(market: ShortCryptoMarket, config: Btc5mMomentumConfig, now_ts: float) -> str | None:
    if market.timestamp is None or now_ts - float(market.timestamp) > config.stale_quote_sec:
        return "quote_stale"
    spread = _spread(market)
    if spread is None or spread > config.max_spread:
        return "spread_too_wide"
    if _liquidity(market) < config.min_liquidity:
        return "liquidity_too_low"
    return None


def _rejected_intent(market: ShortCryptoMarket, metadata: dict[str, Any], reason: str, now_ts: float) -> dict[str, Any]:
    enriched = dict(metadata)
    enriched["skip_reason"] = reason
    return build_paper_intent(
        market,
        selected_side=str(market.direction or "unknown").lower(),
        metadata=enriched,
        config=Btc5mMomentumConfig(),
        now_ts=now_ts,
    )


def _base_metadata(*, seconds_to_expiry: float, config: Btc5mMomentumConfig) -> dict[str, Any]:
    would_force = seconds_to_expiry <= config.exit_before_sec
    return {
        "seconds_to_expiry": seconds_to_expiry,
        "would_exit_before_expiry": True,
        "would_force_close": would_force,
        "exit_reason": "force_close_at_exit_before_sec" if would_force else "exit_before_expiry_scheduled",
        "skip_reason": None,
    }


def _impulse_payload(
    bucket_open: float | None,
    current: float | None,
    impulse_abs: float | None,
    impulse_pct: float | None,
    confirmed: bool,
    skip_reason: str | None,
) -> dict[str, Any]:
    return {
        "btc_bucket_open": bucket_open,
        "btc_current": current,
        "btc_impulse_abs": impulse_abs,
        "btc_impulse_pct": impulse_pct,
        "impulse_confirmed": confirmed,
        "skip_reason": skip_reason,
    }


def _seconds_to_expiry(market: ShortCryptoMarket, now_ts: float) -> float:
    return float(market.end_ts or now_ts) - now_ts


def _ask(market: ShortCryptoMarket) -> float | None:
    return float(market.yes_ask) if market.yes_ask is not None else None


def _bid(market: ShortCryptoMarket) -> float | None:
    return float(market.yes_bid) if market.yes_bid is not None else None


def _spread(market: ShortCryptoMarket) -> float | None:
    bid = _bid(market)
    ask = _ask(market)
    if bid is None or ask is None:
        return None
    return max(0.0, ask - bid)


def _liquidity(market: ShortCryptoMarket) -> float:
    return float(market.liquidity or 0.0)


def _book(market: ShortCryptoMarket) -> dict[str, Any]:
    bids = []
    asks = []
    if market.yes_bid is not None:
        bids.append({"price": float(market.yes_bid), "size": float(market.liquidity or 0.0)})
    if market.yes_ask is not None:
        asks.append({"price": float(market.yes_ask), "size": float(market.liquidity or 0.0)})
    return {"yes": {"bids": bids, "asks": asks}}


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(float(ts), tz=timezone.utc).isoformat()
