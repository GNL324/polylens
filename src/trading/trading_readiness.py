from __future__ import annotations

import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from src.analysis.paper_copy_trader import DEFAULT_PAPER_COPY_DB, paper_copy_report
from src.risk.engine import RiskEngine
from src.risk.models import RiskConfig as PersistentRiskConfig
from src.trading.kill_switch import KillSwitch
from src.trading.strategy_approval import StrategyApprovalRegistry

DEFAULT_READINESS_DB = "data/trading_readiness.db"
DEFAULT_MIN_PAPER_SAMPLE = 10
DEFAULT_MIN_WIN_RATE = 0.50
DEFAULT_MIN_ROI = 0.0
DEFAULT_MAX_DRAWDOWN = 0.25
DEFAULT_MIN_ALPHA_CONFIDENCE = 0.5

CREDENTIAL_ENV_VARS = (
    "KALSHI_API_KEY",
    "KALSHI_API_SECRET",
    "POLYMARKET_API_KEY",
    "POLYMARKET_PRIVATE_KEY",
    "POLYMARKET_PASSPHRASE",
)


@dataclass
class TradingReadinessReport:
    ready: bool
    blockers: list[str]
    warnings: list[str]
    paper_only: bool
    live_trading_enabled: bool
    required_human_approval: bool
    checks: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return str(value).lower() in {"1", "true", "yes", "on"}


def live_env_flags() -> dict[str, bool]:
    return {
        "LIVE_TRADING": _env_bool("LIVE_TRADING", False),
        "DRY_RUN": _env_bool("DRY_RUN", True),
        "POLYLENS_LIVE_TRADING": _env_bool("POLYLENS_LIVE_TRADING", False),
        "POLYLENS_CONFIRM_RISK_ACK": _env_bool("POLYLENS_CONFIRM_RISK_ACK", False),
        "POLYLENS_KALSHI_LIVE_SENDS_ENABLED": _env_bool("POLYLENS_KALSHI_LIVE_SENDS_ENABLED", False),
        "POLYLENS_POLYMARKET_LIVE_SENDS_ENABLED": _env_bool("POLYLENS_POLYMARKET_LIVE_SENDS_ENABLED", False),
    }


def _credential_presence() -> dict[str, bool]:
    return {name: bool(os.environ.get(name)) for name in CREDENTIAL_ENV_VARS}


def evaluate_trading_readiness(
    *,
    strategy_id: str | None = None,
    paper_copy_db_path: str | Path = DEFAULT_PAPER_COPY_DB,
    risk_db_path: str | Path = "data/polylens.db",
    readiness_db_path: str | Path = DEFAULT_READINESS_DB,
    approved_strategy_id: str | None = None,
    min_paper_sample: int = DEFAULT_MIN_PAPER_SAMPLE,
    min_win_rate: float = DEFAULT_MIN_WIN_RATE,
    min_roi: float = DEFAULT_MIN_ROI,
    max_drawdown: float = DEFAULT_MAX_DRAWDOWN,
    min_alpha_confidence: float = DEFAULT_MIN_ALPHA_CONFIDENCE,
) -> TradingReadinessReport:
    blockers: list[str] = []
    warnings: list[str] = []
    flags = live_env_flags()
    live_trading_enabled = flags["LIVE_TRADING"] and not flags["DRY_RUN"] and flags["POLYLENS_LIVE_TRADING"]

    if live_trading_enabled:
        blockers.append("live trading flags are enabled; framework requires paper-only default")
    if not flags["DRY_RUN"]:
        blockers.append("DRY_RUN must be true by default")
    if flags["LIVE_TRADING"]:
        blockers.append("LIVE_TRADING must be false by default")

    registry = StrategyApprovalRegistry(db_path=readiness_db_path)
    target_strategy = approved_strategy_id or strategy_id
    approval = registry.get_approval(target_strategy) if target_strategy else None
    if target_strategy and (approval is None or approval.get("approval_status") != "approved"):
        blockers.append(f"strategy {target_strategy} is not approved")
    required_human_approval = approval is None or approval.get("approval_status") != "approved"

    copy = paper_copy_report(db_path=paper_copy_db_path)
    by_wallet = copy.get("by_wallet", {})
    total_closed = sum(int(v.get("closed_positions") or 0) for v in by_wallet.values())
    rois = [float(v["roi"]) for v in by_wallet.values() if v.get("roi") is not None]
    win_rates = [float(v["win_rate"]) for v in by_wallet.values() if v.get("win_rate") is not None]
    avg_roi = sum(rois) / len(rois) if rois else 0.0
    avg_win_rate = sum(win_rates) / len(win_rates) if win_rates else 0.0

    if total_closed < min_paper_sample:
        blockers.append(f"insufficient paper sample size ({total_closed} < {min_paper_sample})")
    if win_rates and avg_win_rate < min_win_rate:
        blockers.append(f"win rate below threshold ({avg_win_rate:.4f} < {min_win_rate})")
    if rois and avg_roi < min_roi:
        warnings.append(f"average ROI below target ({avg_roi:.4f} < {min_roi})")

    risk = RiskEngine(db_path=risk_db_path, config=PersistentRiskConfig.from_env())
    risk_status = risk.status()
    if risk_status.get("global_halt"):
        blockers.append("risk engine global halt active")
    daily_pnl = float(risk_status.get("daily_pnl") or 0.0)
    if daily_pnl < -PersistentRiskConfig.from_env().max_daily_loss:
        blockers.append("daily loss cap exceeded")

    kill = KillSwitch(db_path=readiness_db_path)
    if kill.is_halted(scope="global"):
        blockers.append("kill switch global halt active")

    creds = _credential_presence()
    if live_trading_enabled and not any(creds.values()):
        blockers.append("no exchange credentials configured")
    elif not any(creds.values()):
        warnings.append("exchange credentials not configured (required only for future live trading)")

    alpha_confidence = None
    try:
        from src.intelligence.wallet_alpha_lab import WalletAlphaLab

        lab = WalletAlphaLab()
        rankings = lab.rank_wallets(limit=5)
        if rankings:
            alpha_confidence = rankings[0].alpha_confidence
            if alpha_confidence < min_alpha_confidence:
                warnings.append(f"alpha confidence below threshold ({alpha_confidence:.4f})")
    except (ImportError, OSError):
        warnings.append("wallet alpha lab data unavailable")

    cfg = PersistentRiskConfig.from_env()
    if cfg.max_position_size <= 0:
        blockers.append("max position size not configured")
    if cfg.max_daily_loss <= 0:
        blockers.append("daily loss cap not configured")

    strategy_validation_complete = bool(
        target_strategy
        and approval
        and approval.get("approval_status") == "approved"
        and total_closed >= min_paper_sample
    )
    if target_strategy and not strategy_validation_complete:
        blockers.append("strategy validation is not complete")

    # Default readiness (no target strategy) must remain blocked until an explicit
    # strategy is approved. Without a target strategy the framework cannot validate
    # live execution intent.
    if not target_strategy:
        blockers.append("no approved target strategy provided")

    checks = {
        "live_flags": flags,
        "paper_closed_positions": total_closed,
        "avg_roi": round(avg_roi, 4),
        "avg_win_rate": round(avg_win_rate, 4),
        "risk_status": risk_status,
        "credential_presence": creds,
        "alpha_confidence": alpha_confidence,
        "strategy_approval": approval,
        "strategy_validation_complete": strategy_validation_complete,
    }

    paper_only = not live_trading_enabled
    ready = len(blockers) == 0

    return TradingReadinessReport(
        ready=ready,
        blockers=blockers,
        warnings=warnings,
        paper_only=paper_only,
        live_trading_enabled=live_trading_enabled,
        required_human_approval=required_human_approval,
        checks=checks,
    )
