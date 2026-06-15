from __future__ import annotations

from pathlib import Path
from typing import Any

from src.risk.models import RiskConfig as PersistentRiskConfig
from src.trading.execution_gate import default_gate_status
from src.trading.kill_switch import KillSwitch
from src.trading.simulated_order_router import SimulatedOrderRouter
from src.trading.strategy_approval import StrategyApprovalRegistry
from src.trading.trading_readiness import DEFAULT_READINESS_DB, evaluate_trading_readiness, live_env_flags


def build_trading_readiness_dashboard(
    *,
    strategy_id: str | None = None,
    readiness_db_path: str | Path = DEFAULT_READINESS_DB,
    risk_db_path: str | Path = "data/polylens.db",
) -> dict[str, Any]:
    readiness = evaluate_trading_readiness(
        strategy_id=strategy_id,
        readiness_db_path=readiness_db_path,
        risk_db_path=risk_db_path,
        approved_strategy_id=strategy_id,
    )
    gate = default_gate_status()
    kill = KillSwitch(db_path=readiness_db_path).status()
    approvals = StrategyApprovalRegistry(db_path=readiness_db_path).list_approvals()
    orders = SimulatedOrderRouter(db_path=readiness_db_path).load_recent_orders(limit=25)
    flags = live_env_flags()
    cfg = PersistentRiskConfig.from_env()
    return {
        "read_only": True,
        "paper_only": True,
        "live_controls_disabled": True,
        "readiness": readiness.to_dict(),
        "gate": gate.to_dict(),
        "kill_switch": kill,
        "approved_strategies": approvals,
        "simulated_orders": orders,
        "live_flags": flags,
        "risk_limits": {
            "max_daily_loss": cfg.max_daily_loss,
            "max_position_size": cfg.max_position_size,
        },
    }
