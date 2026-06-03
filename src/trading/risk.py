from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


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


def signal_key(signal: dict[str, Any]) -> str:
    return "|".join(str(signal.get(field) or "") for field in ("ticker", "side", "price", "count"))


def evaluate_order(signal: dict[str, Any], config: RiskConfig, state: RiskState | None = None, now: float | None = None) -> RiskDecision:
    state = state or RiskState()
    now = now if now is not None else time.time()
    price = float(signal.get("price") or 0)
    count = int(signal.get("count") or 0)
    trade_dollars = price * count
    if trade_dollars <= 0:
        return RiskDecision(False, "order value must be positive")
    if trade_dollars > config.max_trade_dollars:
        return RiskDecision(False, "max trade dollars exceeded")
    if state.open_exposure + trade_dollars > config.max_open_exposure:
        return RiskDecision(False, "max open exposure exceeded")
    if state.daily_loss >= config.max_daily_loss:
        return RiskDecision(False, "max daily loss exceeded")
    key = signal_key(signal)
    last = state.signal_timestamps.get(key)
    if last is not None and now - last < config.duplicate_signal_cooldown_seconds:
        return RiskDecision(False, "duplicate signal cooldown")
    return RiskDecision(True, "accepted")


def live_trading_enabled(config: RiskConfig) -> bool:
    return config.live_trading and not config.dry_run
