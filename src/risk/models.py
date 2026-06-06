from __future__ import annotations

import os
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


def _bool_env(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None or value == "":
        return default
    return value.lower() in {"1", "true", "yes", "on"}


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _int_env(name: str, default: int) -> int:
    try:
        return int(float(os.environ.get(name, default)))
    except (TypeError, ValueError):
        return default


class RiskDecisionType(str, Enum):
    APPROVE = "APPROVE"
    REJECT = "REJECT"
    HALT = "HALT"
    PAPER_ONLY = "PAPER_ONLY"


@dataclass(frozen=True)
class RiskConfig:
    max_daily_loss: float = 50.0
    max_monthly_loss: float = 250.0
    max_drawdown: float = 100.0
    max_position_size: float = 25.0
    max_exposure_per_venue: float = 100.0
    max_exposure_per_market: float = 50.0
    min_opportunity_score: float = 0.0
    min_expected_edge: float = 0.0
    duplicate_opportunity_cooldown_seconds: int = 900
    dry_run: bool = True
    live_trading: bool = False

    @classmethod
    def from_env(cls) -> "RiskConfig":
        return cls(
            max_daily_loss=_float_env("RISK_MAX_DAILY_LOSS", _float_env("KALSHI_MAX_DAILY_LOSS", 50.0)),
            max_monthly_loss=_float_env("RISK_MAX_MONTHLY_LOSS", 250.0),
            max_drawdown=_float_env("RISK_MAX_DRAWDOWN", 100.0),
            max_position_size=_float_env("RISK_MAX_POSITION_SIZE", _float_env("KALSHI_MAX_TRADE_DOLLARS", 25.0)),
            max_exposure_per_venue=_float_env("RISK_MAX_EXPOSURE_PER_VENUE", _float_env("KALSHI_MAX_OPEN_EXPOSURE", 100.0)),
            max_exposure_per_market=_float_env("RISK_MAX_EXPOSURE_PER_MARKET", 50.0),
            min_opportunity_score=_float_env("RISK_MIN_OPPORTUNITY_SCORE", 0.0),
            min_expected_edge=_float_env("RISK_MIN_EXPECTED_EDGE", 0.0),
            duplicate_opportunity_cooldown_seconds=_int_env("RISK_DUPLICATE_OPPORTUNITY_COOLDOWN_SECONDS", 900),
            dry_run=_bool_env("DRY_RUN", True),
            live_trading=_bool_env("LIVE_TRADING", False),
        )


@dataclass(frozen=True)
class RiskRequest:
    venue: str
    market: str
    proposed_stake: float
    opportunity_id: str | None = None
    score: float | None = None
    edge: float | None = None
    dry_run: bool | None = None
    live_trading: bool | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RiskDecision:
    decision: RiskDecisionType
    approved: bool
    reason: str
    venue: str
    market: str
    opportunity_id: str | None = None
    score: float | None = None
    edge: float | None = None
    proposed_stake: float = 0.0
    current_exposure: float = 0.0
    daily_pnl: float = 0.0
    monthly_pnl: float = 0.0
    dry_run: bool = True
    live_trading: bool = False

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["decision"] = self.decision.value
        return data
