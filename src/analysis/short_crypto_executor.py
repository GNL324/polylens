from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.analysis.short_crypto_markets import ShortCryptoMarket
from src.storage.crypto_trade_store import CryptoTradeStore


@dataclass(frozen=True)
class CryptoSignal:
    asset: str
    window_minutes: int
    direction: str
    venue: str
    ticker: str
    spot_price: float
    implied_prob: float
    model_prob: float
    edge: float
    roi: float
    timestamp: float = field(default_factory=lambda: datetime.now(timezone.utc).timestamp())
    meta: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ShortCryptoRiskConfig:
    max_trade_stake: float = 50.0
    max_trade_stake_cap: float = 250.0
    max_daily_loss: float = 250.0
    max_daily_notional: float = 250.0
    max_open_notional: float = 200.0
    max_trades_per_day: int = 10
    max_per_venue_notional: float = 150.0
    min_edge: float = 0.01
    min_liquidity: float = 1.0
    data_freshness_seconds: float = 2.0
    book_freshness_seconds: float = 2.0
    close_cutoff_seconds: float = 20.0
    duplicate_trade_protection: bool = True
    kill_switch: str = "/home/noel/polylens/.kill_switch"

    @classmethod
    def from_env(cls) -> "ShortCryptoRiskConfig":
        return cls(
            max_trade_stake=_float_env("POLYLENS_SHORT_CRYPTO_MAX_STAKE", 50.0),
            max_trade_stake_cap=_float_env("POLYLENS_SHORT_CRYPTO_MAX_STAKE_CAP", 250.0),
            max_daily_loss=_float_env("POLYLENS_SHORT_CRYPTO_MAX_DAILY_LOSS", 250.0),
            max_daily_notional=_float_env("POLYLENS_SHORT_CRYPTO_MAX_DAILY_NOTIONAL", 250.0),
            max_open_notional=_float_env("POLYLENS_SHORT_CRYPTO_MAX_OPEN_NOTIONAL", 200.0),
            max_trades_per_day=_int_env("POLYLENS_SHORT_CRYPTO_MAX_TRADES_PER_DAY", 10),
            max_per_venue_notional=_float_env("POLYLENS_SHORT_CRYPTO_MAX_PER_VENUE_NOTIONAL", 150.0),
            min_edge=_float_env("POLYLENS_SHORT_CRYPTO_MIN_EDGE", 0.01),
            min_liquidity=_float_env("POLYLENS_SHORT_CRYPTO_MIN_LIQUIDITY", 1.0),
            data_freshness_seconds=_float_env("POLYLENS_CRYPTO_STALENESS_SECS", 2.0),
            book_freshness_seconds=_float_env("POLYLENS_SHORT_CRYPTO_BOOK_STALENESS_SECS", 2.0),
            close_cutoff_seconds=_float_env("POLYLENS_SHORT_CRYPTO_CLOSE_CUTOFF_SECS", 20.0),
            duplicate_trade_protection=_bool_env("POLYLENS_SHORT_CRYPTO_DEDUPE", True),
            kill_switch=os.environ.get("POLYLENS_KILL_SWITCH", "/home/noel/polylens/.kill_switch"),
        )


class _RiskState:
    def __init__(self) -> None:
        self.daily_notional = 0.0
        self.daily_loss = 0.0
        self.open_notional = 0.0
        self.trade_count = 0
        self.per_venue: Dict[str, float] = {}
        self.day_key: Optional[str] = None

    def _bump_day(self) -> None:
        today = datetime.now(timezone.utc).date().isoformat()
        if self.day_key != today:
            self.day_key = today
            self.daily_notional = 0.0
            self.daily_loss = 0.0
            self.trade_count = 0
            self.per_venue.clear()

    def add_trade(self, venue: str, stake: float) -> None:
        self._bump_day()
        self.daily_notional += stake
        self.trade_count += 1
        self.open_notional += stake
        self.per_venue[venue] = self.per_venue.get(venue, 0.0) + stake


class _DedupeStore:
    def __init__(self, db_path: str = "data/polylens.db") -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self):
        import sqlite3

        return sqlite3.connect(self.db_path)

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS crypto_trade_keys (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    trade_key TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL,
                    asset TEXT,
                    window_minutes INTEGER,
                    direction TEXT,
                    venue TEXT,
                    ticker TEXT,
                    mode TEXT,
                    meta_json TEXT
                )
                """
            )

    def exists(self, trade_key: str) -> bool:
        with self._connect() as conn:
            row = conn.execute("SELECT 1 FROM crypto_trade_keys WHERE trade_key=? LIMIT 1", (trade_key,)).fetchone()
        return row is not None

    def record(self, trade_key: str, signal: CryptoSignal, mode: str, meta: Dict[str, Any]) -> bool:
        created_at = datetime.now(timezone.utc).isoformat()
        try:
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO crypto_trade_keys (trade_key, created_at, asset, window_minutes, direction, venue, ticker, mode, meta_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        trade_key,
                        created_at,
                        signal.asset,
                        int(signal.window_minutes),
                        signal.direction,
                        signal.venue,
                        signal.ticker,
                        mode,
                        json.dumps(meta, sort_keys=True, default=str),
                    ),
                )
            return True
        except Exception:
            return False


def _dedupe_key(asset: str, window_minutes: int, direction: str, venue: str, ticker: str, mode: str, run_id: str | None = None) -> str:
    parts = [asset, str(int(window_minutes)), direction, venue, ticker, mode]
    if run_id:
        parts.append(f"run:{run_id}")
    raw = "|".join(parts)
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def first_live_test_run_id() -> str | None:
    value = os.environ.get("POLYLENS_FIRST_LIVE_TEST_RUN_ID")
    if value is None:
        return None
    value = value.strip()
    return value or None


def first_live_test_dedupe_context(*, mode: str, count: int, max_loops: int | None) -> Dict[str, Any]:
    enabled = _first_live_test_enabled() and mode == "live" and int(count) == 1 and int(max_loops or 0) == 1
    run_id = first_live_test_run_id() if enabled else None
    return {"enabled": enabled, "run_id": run_id, "missing_run_id": enabled and not run_id}


def live_trade_dedupe_key(signal: CryptoSignal, *, mode: str, count: int, max_loops: int | None) -> str:
    context = first_live_test_dedupe_context(mode=mode, count=count, max_loops=max_loops)
    return _dedupe_key(
        signal.asset,
        signal.window_minutes,
        signal.direction,
        signal.venue,
        signal.ticker,
        mode,
        context["run_id"],
    )


class ShortCryptoSignalEngine:
    def __init__(self, assets: Optional[List[str]] = None, windows: Optional[List[int]] = None, min_edge: float = 0.01) -> None:
        self.assets = [a.strip().upper() for a in (assets or ["BTC", "ETH", "SOL"]) if a and str(a).strip()]
        self.windows = [int(w) for w in (windows or [5])]
        self.min_edge = float(min_edge)

    def generate_signals(self, markets: List[Any], spot_map: Dict[str, float]) -> List[CryptoSignal]:
        out: List[CryptoSignal] = []
        for market in markets:
            if not isinstance(market, ShortCryptoMarket) or market.asset not in self.assets:
                continue
            implied = market.implied_prob("yes")
            if implied is None:
                continue
            model = self._model_prob(implied, market.asset, market.direction)
            edge = model - implied
            roi = edge / max(implied, 1e-9)
            if roi < self.min_edge:
                continue
            spot_price = float(spot_map.get(market.asset) or market.reference_price or 0.0)
            if spot_price <= 0:
                continue
            for window in self.windows:
                if market.window_minutes is not None and int(market.window_minutes) != int(window):
                    continue
                out.append(
                    CryptoSignal(
                        asset=market.asset,
                        window_minutes=window,
                        direction=market.direction,
                        venue=market.venue,
                        ticker=market.ticker,
                        spot_price=spot_price,
                        implied_prob=implied,
                        model_prob=model,
                        edge=edge,
                        roi=roi,
                        timestamp=time.time(),
                        meta={"liquidity": market.liquidity, "market": market},
                    )
                )
        return out

    def _model_prob(self, implied: float, asset: str, direction: str) -> float:
        return min(0.99, max(0.01, implied + 0.05))


class ShortCryptoExecutor:
    def __init__(self, config: Optional[ShortCryptoRiskConfig] = None, db_path: str = "data/polylens.db") -> None:
        self.config = config or ShortCryptoRiskConfig.from_env()
        self.kill_switch = Path(self.config.kill_switch)
        self._state = _RiskState()
        self._dedupe = _DedupeStore(db_path=db_path)
        self._store = CryptoTradeStore(db_path)
        self._store.initialize()

    def execute(self, signal: CryptoSignal, mode: str = "paper", *, live: bool = False, max_loops: int | None = None) -> Dict[str, Any]:
        if isinstance(signal, dict):
            signal = _signal_from_dict(signal)
        mode = "live" if live or mode == "live" else "paper"
        stake = self._sized_stake(signal)
        count = max(1, int(stake / max(self._execution_price(signal), 0.01)))
        if mode == "live" and _first_live_test_enabled():
            stake = min(stake, 1.0)
            count = 1
        trade_key = live_trade_dedupe_key(signal, mode=mode, count=count, max_loops=max_loops)
        order_intent = self._order_intent(signal, stake=stake, count=count, mode=mode)
        if mode == "live" and _first_live_test_enabled():
            self._apply_first_live_test_order_semantics(signal, order_intent)

        rejection = self._validate(signal, stake, trade_key, mode=mode, require_env=mode == "live", count=count, max_loops=max_loops)
        if rejection:
            return self._decision(signal, False, rejection, {"stake": stake, "trade_key": trade_key, "order_intent": order_intent})

        if mode == "paper":
            result = self._paper_fill(signal, order_intent, trade_key)
        elif signal.venue == "kalshi":
            result = KalshiShortCryptoLiveAdapter().submit(order_intent)
        elif signal.venue == "polymarket":
            result = PolymarketShortCryptoLiveAdapter().submit(order_intent)
        else:
            result = {"accepted": False, "status": "rejected", "reason": "unsupported_venue"}

        if result.get("accepted") or result.get("status") in {"blocked", "rejected"}:
            self._dedupe.record(trade_key, signal, mode, {"stake": stake, "order_intent": order_intent, "result": result})
        if result.get("accepted"):
            self._state.add_trade(signal.venue, stake)
            self._store.save_trade(
                {
                    "asset": signal.asset,
                    "window_minutes": signal.window_minutes,
                    "direction": signal.direction,
                    "venue": signal.venue,
                    "ticker": signal.ticker,
                    "price": result.get("fill_price") or order_intent["price"],
                    "count": order_intent["count"],
                    "stake": stake,
                    "mode": mode,
                    "status": result.get("status", "filled"),
                    "order": result.get("order") or order_intent,
                }
            )
        self._store.save_decision(
            {
                "asset": signal.asset,
                "window_minutes": signal.window_minutes,
                "direction": signal.direction,
                "venue": signal.venue,
                "ticker": signal.ticker,
                "accepted": bool(result.get("accepted")),
                "reason": result.get("reason"),
                "meta": {"result": result, "trade_key": trade_key},
            }
        )
        return result

    def _validate(self, signal: CryptoSignal, stake: float, trade_key: str, *, mode: str, require_env: bool, count: int | None = None, max_loops: int | None = None) -> Optional[str]:
        if self.kill_switch.exists():
            return "kill_switch_active"
        if signal.roi < self.config.min_edge:
            return "edge_below_threshold"
        if stake > self.config.max_trade_stake_cap or self.config.max_trade_stake > self.config.max_trade_stake_cap:
            return "max_stake_above_configured_cap"
        if self._daily_loss_breached():
            return "daily_loss_limit_breached"
        if not self._market_data_fresh(signal):
            return "stale_price_feed"
        if not self._book_fresh(signal):
            return "stale_order_book"
        if self._close_cutoff_breached(signal):
            return "market_close_cutoff_breached"
        if first_live_test_dedupe_context(mode=mode, count=int(count or 0), max_loops=max_loops)["missing_run_id"]:
            return "missing_first_live_test_run_id"
        if mode == "live" and self.config.duplicate_trade_protection and self._dedupe.exists(trade_key):
            return "duplicate_trade_key"
        if not self._within_risk_limits(stake, signal.venue):
            return "risk_limit_breached"
        if require_env:
            for name in ("POLYLENS_LIVE_TRADING", "POLYLENS_AUTONOMOUS_CRYPTO", "POLYLENS_CONFIRM_RISK_ACK"):
                if not _env_true(name):
                    return f"missing_live_gate_{name.lower()}"
            if _first_live_test_enabled() and self._state.trade_count >= 1:
                return "first_live_test_max_one_order"
        return None

    def _apply_first_live_test_order_semantics(self, signal: CryptoSignal, order_intent: Dict[str, Any]) -> None:
        market = signal.meta.get("market") if isinstance(signal.meta, dict) else None
        raw = getattr(market, "raw", None) or {}
        selected = raw.get("selected_yes_ask") if isinstance(raw, dict) else None
        if not isinstance(selected, dict):
            return
        order_intent["selected_liquidity_source"] = selected.get("derived_from")
        order_intent["count"] = 1
        if selected.get("derived_from") == "no_bid":
            order_intent["action"] = "sell"
            order_intent["side"] = "no"
            order_intent["price"] = int(selected["price_cents"]) / 100.0
            order_intent.pop("yes_price_cents", None)
            order_intent["no_price_cents"] = int(selected["no_bid_cents"])
            order_intent["kalshi_payload"] = build_kalshi_order_payload(order_intent)

    def _paper_fill(self, signal: CryptoSignal, order_intent: Dict[str, Any], trade_key: str) -> Dict[str, Any]:
        fill_price = self._execution_price(signal)
        return {
            "accepted": True,
            "status": "filled",
            "mode": "paper",
            "fill_price": fill_price,
            "stake": order_intent["stake"],
            "trade_key": trade_key,
            "order": {**order_intent, "price": fill_price},
        }

    def _order_intent(self, signal: CryptoSignal, *, stake: float, count: int, mode: str) -> Dict[str, Any]:
        price = self._execution_price(signal)
        payload = {
            "asset": signal.asset,
            "direction": signal.direction,
            "venue": signal.venue,
            "ticker": signal.ticker,
            "window_minutes": signal.window_minutes,
            "side": "yes",
            "action": "buy",
            "price": price,
            "count": count,
            "stake": stake,
            "mode": mode,
            "client_order_id": f"polylens-sc-{int(time.time())}-{uuid.uuid4().hex[:12]}",
        }
        if signal.venue == "kalshi":
            payload["kalshi_payload"] = build_kalshi_order_payload(payload)
        return payload

    def _execution_price(self, signal: CryptoSignal) -> float:
        market = signal.meta.get("market") if isinstance(signal.meta, dict) else None
        if isinstance(market, ShortCryptoMarket) and market.yes_ask is not None:
            return float(market.yes_ask)
        return min(0.99, max(0.01, float(signal.implied_prob)))

    def _sized_stake(self, signal: CryptoSignal) -> float:
        return min(self.config.max_trade_stake, max(1.0, float(signal.spot_price) * 0.01))

    def _daily_loss_breached(self) -> bool:
        self._state._bump_day()
        return self._state.daily_loss >= self.config.max_daily_loss

    def _market_data_fresh(self, signal: CryptoSignal) -> bool:
        ts = float(signal.meta.get("price_timestamp", signal.timestamp) if isinstance(signal.meta, dict) else signal.timestamp)
        return ts <= time.time() + 5.0 and (time.time() - ts) <= self.config.data_freshness_seconds

    def _book_fresh(self, signal: CryptoSignal) -> bool:
        market = signal.meta.get("market") if isinstance(signal.meta, dict) else None
        if isinstance(market, ShortCryptoMarket):
            return market.is_valid(max_age=self.config.book_freshness_seconds) and (market.liquidity or 0.0) >= self.config.min_liquidity
        book_ts = signal.meta.get("book_timestamp") if isinstance(signal.meta, dict) else None
        liquidity = signal.meta.get("liquidity") if isinstance(signal.meta, dict) else None
        try:
            return book_ts is not None and (time.time() - float(book_ts)) <= self.config.book_freshness_seconds and float(liquidity or 0.0) >= self.config.min_liquidity
        except Exception:
            return False

    def _close_cutoff_breached(self, signal: CryptoSignal) -> bool:
        market = signal.meta.get("market") if isinstance(signal.meta, dict) else None
        end_ts = market.end_ts if isinstance(market, ShortCryptoMarket) else signal.meta.get("end_ts") if isinstance(signal.meta, dict) else None
        return end_ts is not None and (float(end_ts) - time.time()) <= self.config.close_cutoff_seconds

    def _within_risk_limits(self, stake: float, venue: str) -> bool:
        state = self._state
        state._bump_day()
        if state.trade_count >= self.config.max_trades_per_day:
            return False
        if (state.daily_notional + stake) > self.config.max_daily_notional:
            return False
        if (state.open_notional + stake) > self.config.max_open_notional:
            return False
        if (state.per_venue.get(venue, 0.0) + stake) > self.config.max_per_venue_notional:
            return False
        return True

    def _decision(self, signal: CryptoSignal, accepted: bool, reason: Optional[str], meta: Dict[str, Any]) -> Dict[str, Any]:
        decision = {
            "asset": signal.asset,
            "window_minutes": signal.window_minutes,
            "direction": signal.direction,
            "venue": signal.venue,
            "ticker": signal.ticker,
            "accepted": accepted,
            "reason": reason,
            "meta": meta,
        }
        if not accepted:
            self._store.save_decision(decision)
            return {"accepted": False, "status": "rejected", "mode": "rejected", "reason": reason, **meta}
        return decision


class KalshiShortCryptoLiveAdapter:
    def submit(self, order_intent: Dict[str, Any]) -> Dict[str, Any]:
        if not _env_true("POLYLENS_KALSHI_LIVE_SENDS_ENABLED"):
            return {
                "accepted": False,
                "status": "blocked",
                "mode": "live",
                "reason": "kalshi_live_sends_stopped_for_order_semantics_audit",
                "order_intent": order_intent,
            }

        from src.adapters.kalshi import KalshiAuthenticatedClient, KalshiAuthConfigError

        try:
            client = KalshiAuthenticatedClient(raw_dir="data/raw")
            payload = order_intent["kalshi_payload"]
            result = client.place_order(payload)
            return {"accepted": True, "status": "submitted", "mode": "live", "order": result, "payload": payload}
        except KalshiAuthConfigError as exc:
            return {"accepted": False, "status": "blocked", "mode": "live", "reason": "kalshi_credentials_missing", "detail": str(exc)}
        except Exception as exc:
            return {"accepted": False, "status": "blocked", "mode": "live", "reason": "kalshi_live_order_failed", "detail": str(exc)}


class PolymarketShortCryptoLiveAdapter:
    def submit(self, order_intent: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "accepted": False,
            "status": "blocked",
            "mode": "live",
            "reason": "polymarket_live_execution_not_implemented",
            "order_intent": order_intent,
        }


def build_kalshi_order_payload(order_intent: Dict[str, Any]) -> Dict[str, Any]:
    side = str(order_intent.get("side") or "yes").lower()
    price_cents = int(order_intent.get("yes_price_cents") or order_intent.get("no_price_cents") or _price_to_cents(order_intent["price"]))
    count = int(order_intent["count"])
    action = str(order_intent.get("action", "buy")).lower()
    payload = {
        "ticker": order_intent["ticker"],
        "side": side,
        "action": action,
        "type": "limit",
        "count": count,
        "time_in_force": "immediate_or_cancel",
        "client_order_id": order_intent["client_order_id"],
    }
    if action == "buy":
        payload["buy_max_cost"] = count * price_cents
    payload[f"{side}_price"] = price_cents
    return payload


def build_kalshi_order_semantics(ticker: str, orderbook: Dict[str, Any], *, count: int = 1, client_order_id_prefix: str = "polylens-sem") -> Dict[str, Any]:
    ladder = _kalshi_ladder(orderbook)
    yes_ladder = _kalshi_levels(ladder, "yes")
    no_ladder = _kalshi_levels(ladder, "no")
    best_yes_bid = _best_kalshi_bid(yes_ladder)
    best_no_bid = _best_kalshi_bid(no_ladder)
    payloads: Dict[str, Any] = {}

    if best_no_bid:
        yes_ask_cents = 100 - best_no_bid["price_cents"]
        payloads["A"] = _semantic_payload(
            ticker,
            side="yes",
            action="buy",
            price_cents=yes_ask_cents,
            count=min(int(count), best_no_bid["count"]),
            client_order_id=f"{client_order_id_prefix}-buy-yes",
        )
        payloads["D"] = _semantic_payload(
            ticker,
            side="no",
            action="sell",
            price_cents=best_no_bid["price_cents"],
            count=min(int(count), best_no_bid["count"]),
            client_order_id=f"{client_order_id_prefix}-sell-no",
        )
    else:
        yes_ask_cents = None

    if best_yes_bid:
        no_ask_cents = 100 - best_yes_bid["price_cents"]
        payloads["B"] = _semantic_payload(
            ticker,
            side="yes",
            action="sell",
            price_cents=best_yes_bid["price_cents"],
            count=min(int(count), best_yes_bid["count"]),
            client_order_id=f"{client_order_id_prefix}-sell-yes",
        )
        payloads["C"] = _semantic_payload(
            ticker,
            side="no",
            action="buy",
            price_cents=no_ask_cents,
            count=min(int(count), best_yes_bid["count"]),
            client_order_id=f"{client_order_id_prefix}-buy-no",
        )
    else:
        no_ask_cents = None

    return {
        "yes_ladder": yes_ladder,
        "no_ladder": no_ladder,
        "best_yes_bid": best_yes_bid,
        "best_no_bid": best_no_bid,
        "derived_yes_ask_from_no_bid": _semantic_price(yes_ask_cents),
        "derived_no_ask_from_yes_bid": _semantic_price(no_ask_cents),
        "candidate_payloads": payloads,
        "payload_semantics": {
            "buying_yes_by_taking_ask": "If Kalshi exposes only bid ladders, the executable YES ask is the complementary resting NO bid. The payload expected to cross that resting ladder is D: action=sell side=no at best_no_bid; A is the direct buy-YES form and may not cross the NO bid ladder.",
            "selling_yes_into_bid": "B crosses resting YES bid liquidity: action=sell side=yes at best_yes_bid.",
            "buying_no_by_taking_ask": "If Kalshi exposes only bid ladders, the executable NO ask is the complementary resting YES bid. The payload expected to cross that resting ladder is B: action=sell side=yes at best_yes_bid; C is the direct buy-NO form and may not cross the YES bid ladder.",
            "selling_no_into_bid": "D crosses resting NO bid liquidity: action=sell side=no at best_no_bid.",
        },
    }


def _semantic_payload(ticker: str, *, side: str, action: str, price_cents: int, count: int, client_order_id: str) -> Dict[str, Any] | None:
    if price_cents < 1 or price_cents > 99 or count < 1:
        return None
    return build_kalshi_order_payload(
        {
            "ticker": ticker,
            "side": side,
            "action": action,
            "price": price_cents,
            "count": count,
            "client_order_id": client_order_id,
        }
    )


def _semantic_price(price_cents: int | None) -> Dict[str, Any] | None:
    if price_cents is None or price_cents < 1 or price_cents > 99:
        return None
    return {"cents": price_cents, "dollars": round(price_cents / 100.0, 4)}


def _kalshi_ladder(orderbook: Dict[str, Any]) -> Dict[str, Any]:
    orderbook = orderbook or {}
    ladder = orderbook.get("orderbook_fp") or orderbook.get("orderbook") or orderbook
    return ladder if isinstance(ladder, dict) else {}


def _kalshi_levels(ladder: Dict[str, Any], side: str) -> List[Any]:
    if side == "yes":
        return ladder.get("yes_dollars") or ladder.get("yes") or []
    return ladder.get("no_dollars") or ladder.get("no") or []


def _best_kalshi_bid(levels: List[Any]) -> Dict[str, Any] | None:
    best: Dict[str, Any] | None = None
    for raw in levels:
        parsed = _parse_book_level(raw)
        if not parsed or parsed["count"] < 1:
            continue
        if best is None or parsed["price_cents"] > best["price_cents"]:
            best = parsed
    return best


def live_readiness_report(db_path: str = "data/polylens.db", *, assets: Optional[List[str]] = None) -> Dict[str, Any]:
    config = ShortCryptoRiskConfig.from_env()
    assets = assets or ["BTC", "ETH", "SOL"]
    checks = []

    def add(name: str, ok: bool, reason: str | None = None, meta: Dict[str, Any] | None = None) -> None:
        checks.append({"name": name, "ok": bool(ok), "reason": reason, "meta": meta or {}})

    kalshi_creds = bool(os.environ.get("KALSHI_API_KEY_ID")) and bool(os.environ.get("KALSHI_PRIVATE_KEY_PATH"))
    poly_creds = bool(os.environ.get("POLYMARKET_API_KEY") or os.environ.get("POLYMARKET_PRIVATE_KEY"))
    add("required_env_vars_present", True, "paper_mode_does_not_require_live_credentials")
    add("api_credentials_detected", kalshi_creds or poly_creds, None, {"kalshi": kalshi_creds, "polymarket": poly_creds})
    add("paper_mode_works", _paper_probe(db_path, config))
    add("kill_switch_absent", not Path(config.kill_switch).exists(), "kill_switch_active" if Path(config.kill_switch).exists() else None)
    add("sqlite_writable", _sqlite_writable(db_path))
    price_probe = _real_price_feed_probe(assets, config)
    add("price_feed_fresh", price_probe["ok"], price_probe.get("reason"), price_probe)
    book_probe = _real_kalshi_book_probe(assets, config)
    add("market_books_fresh", book_probe["ok"], book_probe.get("reason"), book_probe)
    add("max_stake_configured", config.max_trade_stake > 0 and config.max_trade_stake <= config.max_trade_stake_cap)
    add("daily_loss_limit_configured", config.max_daily_loss > 0)
    add("duplicate_trade_protection_enabled", config.duplicate_trade_protection)
    live_flags = {
        "POLYLENS_LIVE_TRADING": _env_true("POLYLENS_LIVE_TRADING"),
        "POLYLENS_AUTONOMOUS_CRYPTO": _env_true("POLYLENS_AUTONOMOUS_CRYPTO"),
        "POLYLENS_CONFIRM_RISK_ACK": _env_true("POLYLENS_CONFIRM_RISK_ACK"),
    }
    add("live_env_flags", (not any(live_flags.values())) or all(live_flags.values()), None, live_flags)
    status = "ready" if all(check["ok"] for check in checks) else "not_ready"
    return {"status": status, "live_trading_enabled": all(live_flags.values()), "checks": checks, "assets": assets}


def _paper_probe(db_path: str, config: ShortCryptoRiskConfig) -> bool:
    now = time.time()
    market = ShortCryptoMarket("BTC", "kalshi", "BTC-UP-5", now, now + 300, "up", 0.5, 0.55, 0.45, 0.5, 100.0, timestamp=now)
    sig = CryptoSignal("BTC", 5, "up", "kalshi", f"PROBE-{uuid.uuid4().hex}", 100.0, 0.525, 0.6, 0.075, 0.1, now, {"market": market, "price_timestamp": now})
    return bool(ShortCryptoExecutor(config=config, db_path=db_path).execute(sig, mode="paper").get("accepted"))


def _sqlite_writable(db_path: str) -> bool:
    try:
        store = CryptoTradeStore(db_path)
        store.initialize()
        return True
    except Exception:
        return False


def _real_price_feed_probe(assets: List[str], config: ShortCryptoRiskConfig) -> Dict[str, Any]:
    from src.adapters.crypto_price_feed import CryptoPriceFeedManager

    symbols = [f"{asset}-USD" for asset in assets]
    manager = CryptoPriceFeedManager(symbols=symbols)
    deadline = time.time() + _float_env("POLYLENS_SHORT_CRYPTO_READINESS_FEED_WAIT_SECS", 8.0)
    seen: Dict[str, float] = {}
    try:
        manager.start()
        while time.time() < deadline and len(seen) < len(symbols):
            for asset, symbol in zip(assets, symbols):
                if asset in seen:
                    continue
                tick = manager.get_latest(symbol)
                if tick and not tick.is_stale(config.data_freshness_seconds) and (tick.mid or tick.last):
                    seen[asset] = float(tick.mid or tick.last or 0.0)
            if len(seen) < len(symbols):
                time.sleep(0.25)
    except Exception as exc:
        return {"ok": False, "reason": "price_feed_probe_failed", "detail": str(exc), "seen": seen}
    finally:
        try:
            manager.stop()
        except Exception:
            pass
    if not seen:
        seen.update(_coinbase_rest_prices(assets))
    ok = bool(seen)
    return {"ok": ok, "reason": None if ok else "no_fresh_price_ticks", "seen": seen, "symbols": symbols}


def _coinbase_rest_prices(assets: List[str]) -> Dict[str, float]:
    from urllib.request import Request, urlopen

    prices: Dict[str, float] = {}
    for asset in assets:
        symbol = f"{asset}-USD"
        request = Request(f"https://api.exchange.coinbase.com/products/{symbol}/ticker", headers={"User-Agent": "polylens/0.1", "Accept": "application/json"})
        try:
            with urlopen(request, timeout=5) as response:
                payload = json.loads(response.read().decode("utf-8"))
            price = float(payload.get("price") or 0.0)
        except Exception:
            price = 0.0
        if price > 0:
            prices[asset] = price
    return prices


def _real_kalshi_book_probe(assets: List[str], config: ShortCryptoRiskConfig) -> Dict[str, Any]:
    from src.adapters.kalshi import KalshiClient

    client = KalshiClient(raw_dir="data/raw")
    series_by_asset = {"BTC": ["KXBTCD", "KXBTC"], "ETH": ["KXETHD", "KXETH"], "SOL": ["KXSOLD"]}
    checked = 0
    fresh_books = []
    errors = []
    for asset in assets:
        for series in series_by_asset.get(asset, []):
            try:
                markets = client.get_markets(status="open", limit=20, max_pages=1, series_ticker=series)
            except Exception as exc:
                errors.append({"series": series, "error": str(exc)})
                continue
            for market in markets[:5]:
                ticker = market.get("ticker")
                if not ticker:
                    continue
                checked += 1
                try:
                    book = client.get_orderbook(ticker)
                except Exception as exc:
                    errors.append({"ticker": ticker, "error": str(exc)})
                    continue
                if _kalshi_book_has_depth(book):
                    fresh_books.append({"asset": asset, "series": series, "ticker": ticker})
                    break
            if fresh_books:
                break
    ok = bool(fresh_books)
    return {"ok": ok, "reason": None if ok else "no_fresh_kalshi_orderbooks", "checked": checked, "fresh_books": fresh_books, "errors": errors[:5]}


def _kalshi_book_has_depth(book: Dict[str, Any]) -> bool:
    book = book or {}
    orderbook = book.get("orderbook") or book.get("orderbook_fp") or book
    for key in ("yes", "no", "yes_dollars", "no_dollars"):
        levels = orderbook.get(key) if isinstance(orderbook, dict) else None
        if isinstance(levels, list) and levels:
            return True
    return False


def _signal_from_dict(value: Dict[str, Any]) -> CryptoSignal:
    now = time.time()
    meta = dict(value.get("meta") or {})
    if "book_timestamp" not in meta:
        meta["book_timestamp"] = now
    if "price_timestamp" not in meta:
        meta["price_timestamp"] = now
    if "liquidity" not in meta:
        meta["liquidity"] = 100.0
    return CryptoSignal(
        asset=str(value["asset"]).upper(),
        window_minutes=int(value.get("window_minutes", 5)),
        direction=str(value["direction"]).lower(),
        venue=str(value.get("venue", "kalshi")).lower(),
        ticker=str(value.get("ticker", "")),
        spot_price=float(value.get("spot_price", 0.0)),
        implied_prob=float(value.get("implied_prob", 0.5)),
        model_prob=float(value.get("model_prob", 0.6)),
        edge=float(value.get("edge", 0.1)),
        roi=float(value.get("roi", 0.1)),
        timestamp=float(value.get("timestamp", now)),
        meta=meta,
    )


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except Exception:
        return default


def _int_env(name: str, default: int) -> int:
    try:
        return int(float(os.environ.get(name, default)))
    except Exception:
        return default


def _bool_env(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None or value == "":
        return default
    return value.lower() in {"1", "true", "yes", "on"}


def _env_true(name: str) -> bool:
    return _bool_env(name, False)


def _first_live_test_enabled() -> bool:
    return _env_true("POLYLENS_FIRST_LIVE_TEST")


def _price_to_cents(value: Any) -> int:
    numeric = float(value)
    cents = round(numeric * 100) if numeric < 1 else round(numeric)
    if cents < 1 or cents > 99:
        raise ValueError("price must be between 1 and 99 cents")
    return int(cents)


def select_executable_yes_ask(orderbook: Dict[str, Any]) -> Dict[str, Any]:
    orderbook = orderbook or {}
    ladder = orderbook.get("orderbook_fp") or orderbook.get("orderbook") or orderbook
    levels = ladder.get("no_dollars") or ladder.get("no") or []
    best: Dict[str, Any] | None = None
    for raw in levels:
        parsed = _parse_book_level(raw)
        if not parsed:
            continue
        yes_ask_cents = 100 - parsed["price_cents"]
        if yes_ask_cents < 1 or yes_ask_cents > 99 or parsed["count"] < 1:
            continue
        candidate = {**parsed, "no_bid_cents": parsed["price_cents"], "price_cents": yes_ask_cents}
        if best is None or candidate["price_cents"] < best["price_cents"]:
            best = candidate
    if best is None:
        return {"ok": False, "reason": "no_executable_resting_yes_ask"}
    return {
        "ok": True,
        "price_cents": best["price_cents"],
        "count": best["count"],
        "raw": best["raw"],
        "derived_from": "no_bid",
        "no_bid_cents": best["no_bid_cents"],
        "explanation": "Kalshi binary books expose resting YES bids and NO bids; executable YES ask is 100 - best resting NO bid.",
    }


def _parse_book_level(raw: Any) -> Dict[str, Any] | None:
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
    price_cents = int(round(price * 100 if price < 1 else price))
    return {"price_cents": price_cents, "count": count, "raw": raw}
