from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


VALID_SIDES = {"yes", "no"}


def load_env(path: str = ".env") -> dict[str, str]:
    values: dict[str, str] = {}
    env_path = Path(path)
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, value = stripped.split("=", 1)
            values[key.strip()] = value.strip().strip('"').strip("'")
    for key, value in values.items():
        os.environ.setdefault(key, value)
    return values


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _bool_env(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.lower() in {"1", "true", "yes", "on"}


@dataclass
class RiskConfig:
    max_trade_dollars: float = 25.0
    max_open_exposure: float = 100.0
    max_daily_loss: float = 50.0
    duplicate_signal_cooldown_seconds: int = 300
    live_trading: bool = False
    dry_run: bool = True

    @classmethod
    def from_env(cls, env_path: str = ".env") -> "RiskConfig":
        load_env(env_path)
        return cls(
            max_trade_dollars=_float_env("KALSHI_MAX_TRADE_DOLLARS", 25.0),
            max_open_exposure=_float_env("KALSHI_MAX_OPEN_EXPOSURE", 100.0),
            max_daily_loss=_float_env("KALSHI_MAX_DAILY_LOSS", 50.0),
            duplicate_signal_cooldown_seconds=int(_float_env("KALSHI_DUPLICATE_SIGNAL_COOLDOWN_SECONDS", 300)),
            live_trading=_bool_env("LIVE_TRADING", False),
            dry_run=_bool_env("DRY_RUN", True),
        )


@dataclass
class RiskState:
    open_exposure: float = 0.0
    daily_loss: float = 0.0
    signal_timestamps: dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class RiskDecision:
    accepted: bool
    reason: str


def normalize_price(price: Any) -> float:
    try:
        value = float(price)
    except (TypeError, ValueError):
        raise ValueError("price must be numeric") from None
    if value <= 0:
        raise ValueError("price must be positive")
    if value > 100:
        raise ValueError("price must be between 0 and 1 dollars, or 1 and 100 cents")
    if value > 1:
        value = value / 100.0
    if value > 1:
        raise ValueError("price must be 1.00 or lower")
    return round(value, 4)


def normalize_order_signal(signal: dict[str, Any]) -> dict[str, Any]:
    side = str(signal.get("side") or "").lower().strip()
    if side not in VALID_SIDES:
        raise ValueError("side must be yes or no")
    try:
        count = int(signal.get("count"))
    except (TypeError, ValueError):
        raise ValueError("count must be a positive integer") from None
    if count <= 0:
        raise ValueError("count must be a positive integer")
    ticker = str(signal.get("ticker") or "").strip()
    if not ticker:
        raise ValueError("ticker is required")
    return {"ticker": ticker, "side": side, "price": normalize_price(signal.get("price")), "count": count}


def signal_key(signal: dict[str, Any]) -> str:
    normalized = normalize_order_signal(signal)
    return "|".join(str(normalized.get(field) or "") for field in ("ticker", "side", "price", "count"))


def evaluate_order(signal: dict[str, Any], config: RiskConfig, state: RiskState | None = None, now: float | None = None) -> RiskDecision:
    state = state or RiskState()
    now = now if now is not None else time.time()
    try:
        normalized = normalize_order_signal(signal)
    except ValueError as exc:
        return RiskDecision(False, str(exc))
    price = normalized["price"]
    count = normalized["count"]
    trade_dollars = price * count
    if trade_dollars > config.max_trade_dollars:
        return RiskDecision(False, "max trade dollars exceeded")
    if state.open_exposure + trade_dollars > config.max_open_exposure:
        return RiskDecision(False, "max open exposure exceeded")
    if state.daily_loss >= config.max_daily_loss:
        return RiskDecision(False, "max daily loss exceeded")
    key = signal_key(normalized)
    last = state.signal_timestamps.get(key)
    if last is not None and now - last < config.duplicate_signal_cooldown_seconds:
        return RiskDecision(False, "duplicate signal cooldown")
    return RiskDecision(True, "accepted")


def live_trading_enabled(config: RiskConfig) -> bool:
    return config.live_trading and not config.dry_run
