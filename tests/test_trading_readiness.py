from __future__ import annotations

import json
from io import StringIO
import sys

import pytest

from src.trading.execution_gate import check_execution_gate, default_gate_status
from src.trading.kill_switch import KillSwitch
from src.trading.simulated_order_router import SimulatedOrderRouter
from src.trading.strategy_approval import StrategyApprovalRegistry
from src.trading.trading_readiness import evaluate_trading_readiness, live_env_flags


@pytest.fixture
def readiness_db(tmp_path):
    return tmp_path / "trading_readiness.db"


@pytest.fixture
def risk_db(tmp_path):
    return tmp_path / "polylens.db"


@pytest.fixture(autouse=True)
def safe_env(monkeypatch):
    monkeypatch.delenv("LIVE_TRADING", raising=False)
    monkeypatch.delenv("DRY_RUN", raising=False)
    monkeypatch.delenv("POLYLENS_LIVE_TRADING", raising=False)
    monkeypatch.delenv("POLYLENS_CONFIRM_RISK_ACK", raising=False)


def test_live_env_flags_default_paper_only():
    flags = live_env_flags()
    assert flags["LIVE_TRADING"] is False
    assert flags["DRY_RUN"] is True
    assert flags["POLYLENS_LIVE_TRADING"] is False
    assert flags["POLYLENS_CONFIRM_RISK_ACK"] is False


def test_readiness_blocked_by_default(readiness_db, risk_db):
    report = evaluate_trading_readiness(
        readiness_db_path=readiness_db,
        risk_db_path=risk_db,
        min_paper_sample=1,
    )
    assert report.ready is False
    assert report.paper_only is True
    assert report.live_trading_enabled is False
    assert report.required_human_approval is True
    assert report.blockers


def test_readiness_blocks_live_flags(monkeypatch, readiness_db, risk_db):
    monkeypatch.setenv("LIVE_TRADING", "true")
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("POLYLENS_LIVE_TRADING", "true")
    report = evaluate_trading_readiness(readiness_db_path=readiness_db, risk_db_path=risk_db)
    assert report.live_trading_enabled is True
    assert any("live trading flags" in item for item in report.blockers)


def test_strategy_approval_registry(readiness_db):
    registry = StrategyApprovalRegistry(db_path=readiness_db)
    approved = registry.approve_strategy(
        "strategy-alpha",
        approved_by="operator",
        max_capital=1000,
        max_position_size=50,
        allowed_markets=["market-1"],
        notes="paper validated",
    )
    assert approved["approval_status"] == "approved"
    assert approved["allowed_markets"] == ["market-1"]

    listed = registry.list_approvals(status="approved")
    assert len(listed) == 1

    revoked = registry.revoke_strategy("strategy-alpha", notes="expired")
    assert revoked["approval_status"] == "revoked"


def test_execution_gate_blocked_by_default(readiness_db, risk_db):
    gate = default_gate_status()
    assert gate.blocked is True
    assert gate.allowed is False
    assert gate.reasons


def test_execution_gate_requires_all_conditions(monkeypatch, readiness_db, risk_db):
    registry = StrategyApprovalRegistry(db_path=readiness_db)
    registry.approve_strategy("s1", approved_by="ops")
    monkeypatch.setenv("LIVE_TRADING", "true")
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("POLYLENS_CONFIRM_RISK_ACK", "true")
    monkeypatch.setenv("KALSHI_MAX_DAILY_LOSS", "100")
    monkeypatch.setenv("KALSHI_MAX_TRADE_DOLLARS", "25")

    gate = check_execution_gate(
        strategy_id="s1",
        readiness_db_path=readiness_db,
        risk_db_path=risk_db,
    )
    assert gate.blocked is True
    assert gate.allowed is False


def test_kill_switch_global_halt_and_resume(readiness_db):
    kill = KillSwitch(db_path=readiness_db)
    assert kill.is_halted(scope="global") is False
    kill.halt(scope="global", reason="test halt")
    assert kill.is_halted(scope="global") is True
    assert kill.status()["halted"] is True
    kill.resume(scope="global", reason="test resume")
    assert kill.is_halted(scope="global") is False


def test_kill_switch_strategy_scope(readiness_db):
    kill = KillSwitch(db_path=readiness_db)
    kill.halt(scope="strategy", strategy_id="s1", reason="strategy halt")
    assert kill.is_halted(scope="strategy", strategy_id="s1") is True
    assert kill.is_halted(scope="strategy", strategy_id="s2") is False


def test_simulated_order_router_rejects_without_gate(readiness_db):
    router = SimulatedOrderRouter(db_path=readiness_db)
    result = router.route_order(
        exchange="kalshi",
        market="TEST-MARKET",
        side="yes",
        price=0.55,
        size=10,
        strategy_id="missing",
    )
    assert result["paper_only"] is True
    assert result["accepted"] is False
    assert result["status"] == "rejected"
    assert result["rejection_reason"]

    orders = router.load_recent_orders(limit=5)
    assert len(orders) == 1
    assert orders[0]["status"] == "rejected"


def test_trading_readiness_cli_json(readiness_db, risk_db, monkeypatch):
    from src.cli import trading_readiness_cli

    monkeypatch.setenv("POLYLENS_DB_PATH", str(risk_db))
    result = trading_readiness_cli(
        as_json=True,
        readiness_db_path=str(readiness_db),
        risk_db_path=str(risk_db),
    )
    assert result["read_only"] is True
    assert result["paper_only"] is True
    assert "report" in result


def test_trading_gate_check_cli(readiness_db, risk_db):
    from src.cli import trading_gate_check_cli

    result = trading_gate_check_cli(
        as_json=True,
        readiness_db_path=str(readiness_db),
        risk_db_path=str(risk_db),
    )
    assert result["gate"]["blocked"] is True


def test_strategy_approve_and_list_cli(readiness_db):
    from src.cli import strategy_approve_cli, strategy_approvals_cli, strategy_revoke_cli

    strategy_approve_cli(
        strategy_id="cli-strategy",
        approved_by="tester",
        as_json=True,
        readiness_db_path=str(readiness_db),
    )
    listed = strategy_approvals_cli(as_json=True, readiness_db_path=str(readiness_db))
    assert len(listed["approvals"]) == 1
    strategy_revoke_cli(strategy_id="cli-strategy", as_json=True, readiness_db_path=str(readiness_db))


def test_simulated_order_cli(readiness_db):
    from src.cli import simulated_order_cli

    result = simulated_order_cli(
        exchange="kalshi",
        market="MKT",
        side="yes",
        price=0.5,
        size=5,
        as_json=True,
        readiness_db_path=str(readiness_db),
    )
    assert result["status"] == "rejected"


def test_trading_kill_resume_status_cli(readiness_db, risk_db):
    from src.cli import trading_kill_cli, trading_resume_cli, trading_status_cli

    trading_kill_cli(as_json=True, readiness_db_path=str(readiness_db), reason="cli test")
    status = trading_status_cli(
        as_json=True,
        readiness_db_path=str(readiness_db),
        risk_db_path=str(risk_db),
    )
    assert status["kill_switch"]["halted"] is True
    trading_resume_cli(as_json=True, readiness_db_path=str(readiness_db), reason="cli resume")
    status = trading_status_cli(
        as_json=True,
        readiness_db_path=str(readiness_db),
        risk_db_path=str(risk_db),
    )
    assert status["kill_switch"]["halted"] is False


def test_trading_readiness_cli_human_output(readiness_db, risk_db, monkeypatch):
    from src.cli import trading_readiness_cli

    captured = StringIO()
    monkeypatch.setattr(sys, "stdout", captured)
    trading_readiness_cli(
        as_json=False,
        readiness_db_path=str(readiness_db),
        risk_db_path=str(risk_db),
    )
    output = captured.getvalue()
    assert "Trading readiness:" in output
    assert "Blockers:" in output


def test_trading_readiness_dashboard_payload(readiness_db, risk_db):
    from src.web.trading_readiness_data import build_trading_readiness_dashboard

    payload = build_trading_readiness_dashboard(
        readiness_db_path=readiness_db,
        risk_db_path=risk_db,
    )
    assert payload["read_only"] is True
    assert payload["live_controls_disabled"] is True
    assert "readiness" in payload
    assert "gate" in payload
    assert payload["gate"]["blocked"] is True


def test_trader_dashboard_includes_trading_readiness_nav():
    from src.web.trader_dashboard_styles import TRADER_NAV_ITEMS

    assert "Trading Readiness" in TRADER_NAV_ITEMS
