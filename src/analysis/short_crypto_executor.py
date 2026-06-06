from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

LOGGER = logging.getLogger(__name__)

from src.analysis.short_crypto_markets import ShortCryptoMarket


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
    max_daily_notional: float = 250.0
    max_open_notional: float = 200.0
    max_trades_per_day: int = 10
    max_per_venue_notional: float = 150.0
    min_edge: float = 0.01
    data_freshness_seconds: float = 2.0
    kill_switch: str = "/home/noel/polylens/.kill_switch"


class _RiskState:
    def __init__(self) -> None:
        self.daily_notional: float = 0.0
        self.open_notional: float = 0.0
        self.trade_count: int = 0
        self.per_venue: Dict[str, float] = {}
        self.day_key: Optional[str] = None

    def _bump_day(self) -> None:
        today = datetime.now(timezone.utc).date().isoformat()
        if self.day_key != today:
            self.day_key = today
            self.daily_notional = 0.0
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


def _dedupe_key(asset: str, window_minutes: int, direction: str, venue: str, ticker: str, mode: str) -> str:
    raw = "|".join([asset, str(int(window_minutes)), direction, venue, ticker, mode])
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


class ShortCryptoSignalEngine:
    def __init__(self, assets: Optional[List[str]] = None, windows: Optional[List[int]] = None, min_edge: float = 0.01) -> None:
        self.assets = [a.strip().upper() for a in (assets or ["BTC", "ETH", "SOL"]) if a and str(a).strip()]
        self.windows = [int(w) for w in (windows or [5])]
        self.min_edge = float(min_edge)

    def generate_signals(self, markets: List[Any], spot_map: Dict[str, float]) -> List[CryptoSignal]:
        spots: Dict[str, float] = {}
        for asset in self.assets:
            price = spot_map.get(asset)
            if isinstance(price, (int, float)) and price > 0:
                spots[asset] = float(price)

        out: List[CryptoSignal] = []
        for market in markets:
            if not isinstance(market, ShortCryptoMarket):
                continue
            if market.asset not in self.assets:
                continue
            implied = market.implied_prob("yes")
            if implied is None:
                continue
            model = self._model_prob(implied, market.asset, market.direction)
            edge = model - implied
            roi = edge / max(implied, 1e-9)
            if roi < self.min_edge:
                continue
            spot_price = spots.get(market.asset, 0.0)
            if spot_price <= 0:
                continue
            for window in self.windows:
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
                        meta={"liquidity": market.liquidity},
                    )
                )
        return out

    def _model_prob(self, implied: float, asset: str, direction: str) -> float:
        return min(0.99, max(0.01, implied + 0.05))


class ShortCryptoExecutor:
    def __init__(self, config: Optional[ShortCryptoRiskConfig] = None, db_path: str = "data/polylens.db") -> None:
        self.config = config or ShortCryptoRiskConfig()
        self.kill_switch = Path(self.config.kill_switch)
        self._state = _RiskState()
        self._dedupe = _DedupeStore(db_path=db_path)

    def execute(self, signal: CryptoSignal, mode: str = "paper") -> Dict[str, Any]:
        if mode != "paper":
            raise RuntimeError("only paper mode supported in this release")
        if self.kill_switch.exists():
            return {"accepted": False, "mode": "rejected", "reason": "kill switch active"}
        if signal.roi < self.config.min_edge:
            return {"accepted": False, "mode": "rejected", "reason": "edge below threshold", "roi": signal.roi}
        stake = self._sized_stake(signal)
        duplicate = not self._dedupe.record(
            trade_key=_dedupe_key(signal.asset, signal.window_minutes, signal.direction, signal.venue, signal.ticker, mode),
            signal=signal,
            mode=mode,
            meta={"stake": stake},
        )
        if duplicate:
            return {"accepted": False, "mode": "rejected", "reason": "duplicate execution blocked"}
        if not self._within_risk_limits(stake, signal.venue):
            return {"accepted": False, "mode": "rejected", "reason": "risk limit breached"}
        self._state.add_trade(signal.venue, stake)
        return {
            "accepted": True,
            "mode": "paper",
            "order": {
                "asset": signal.asset,
                "direction": signal.direction,
                "venue": signal.venue,
                "ticker": signal.ticker,
                "stake": stake,
            },
            "stake": stake,
        }

    def _sized_stake(self, signal: CryptoSignal) -> float:
        return min(self.config.max_trade_stake, max(1.0, float(signal.spot_price) * 0.01))

    def _within_risk_limits(self, stake: float, venue: str) -> bool:
        state = self._state
        state._bump_day()
        if state.trade_count >= self.config.max_trades_per_day:
            return False
        if (state.daily_notional + stake) > self.config.max_daily_notional:
            return False
        if (state.open_notional + stake) > self.config.max_open_notional:
            return False
        venue_current = state.per_venue.get(venue, 0.0)
        if (venue_current + stake) > self.config.max_per_venue_notional:
            return False
        return True
