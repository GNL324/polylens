from __future__ import annotations

import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.trading.kill_switch import KillSwitch
from src.trading.strategy_approval import StrategyApprovalRegistry
from src.trading.trading_readiness import evaluate_trading_readiness, live_env_flags

DEFAULT_READINESS_DB = "data/trading_readiness.db"


@dataclass
class ExecutionGateResult:
    allowed: bool
    blocked: bool
    reasons: list[str]
    checks: dict[str, bool] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def check_execution_gate(
    *,
    strategy_id: str | None = None,
    exchange: str = "kalshi",
    market: str = "",
    readiness_db_path: str | Path = DEFAULT_READINESS_DB,
    risk_db_path: str | Path = "data/polylens.db",
    has_open_position: bool = False,
    market_data_stale: bool = False,
    sre_incident_open: bool = False,
) -> ExecutionGateResult:
    flags = live_env_flags()
    reasons: list[str] = []
    checks: dict[str, bool] = {}

    checks["live_trading_true"] = flags["LIVE_TRADING"]
    checks["dry_run_false"] = not flags["DRY_RUN"]
    checks["risk_ack"] = flags["POLYLENS_CONFIRM_RISK_ACK"]
    checks["polylens_live_trading"] = flags["POLYLENS_LIVE_TRADING"]

    if not flags["LIVE_TRADING"]:
        reasons.append("LIVE_TRADING is not true")
    if flags["DRY_RUN"]:
        reasons.append("DRY_RUN is not false")
    if not flags["POLYLENS_CONFIRM_RISK_ACK"]:
        reasons.append("POLYLENS_CONFIRM_RISK_ACK is not true")
    if not strategy_id:
        reasons.append("approved_strategy_id is not set")
    else:
        approval = StrategyApprovalRegistry(db_path=readiness_db_path).get_approval(strategy_id)
        checks["strategy_approved"] = approval is not None and approval.get("approval_status") == "approved"
        if not checks["strategy_approved"]:
            reasons.append(f"strategy {strategy_id} is not approved")

    readiness = evaluate_trading_readiness(
        strategy_id=strategy_id,
        readiness_db_path=readiness_db_path,
        risk_db_path=risk_db_path,
        approved_strategy_id=strategy_id,
    )
    checks["readiness_passed"] = readiness.ready
    if not readiness.ready:
        reasons.extend(readiness.blockers)

    max_position = float(os.environ.get("KALSHI_MAX_TRADE_DOLLARS", os.environ.get("POLYLENS_MAX_POSITION_SIZE", "0")) or 0)
    max_daily_loss = float(os.environ.get("KALSHI_MAX_DAILY_LOSS", os.environ.get("POLYLENS_MAX_DAILY_LOSS", "0")) or 0)
    checks["daily_loss_cap_configured"] = max_daily_loss > 0
    checks["max_position_configured"] = max_position > 0
    if max_daily_loss <= 0:
        reasons.append("daily loss cap not configured")
    if max_position <= 0:
        reasons.append("max position size not configured")

    kill = KillSwitch(db_path=readiness_db_path)
    checks["kill_switch_healthy"] = not kill.is_halted(scope="global", strategy_id=strategy_id, market=market)
    if not checks["kill_switch_healthy"]:
        reasons.append("kill switch is active")

    checks["market_data_fresh"] = not market_data_stale
    if market_data_stale:
        reasons.append("stale market data")

    checks["no_duplicate_position"] = not has_open_position
    if has_open_position:
        reasons.append("duplicate/open position exists")

    checks["no_sre_incident"] = not sre_incident_open
    if sre_incident_open:
        reasons.append("open SRE incident")

    allowed = len(reasons) == 0
    return ExecutionGateResult(
        allowed=allowed,
        blocked=not allowed,
        reasons=reasons,
        checks=checks,
    )


def default_gate_status() -> ExecutionGateResult:
    """Default state is always blocked."""
    return check_execution_gate(strategy_id=None)
