from __future__ import annotations

import inspect
import json
import sys
from pathlib import Path

from src.analysis import paper_trading_service
from src.analysis.paper_trading_engine import PaperTradingConfig, run_paper_trading_engine, settle_open_positions
from src.analysis.paper_trading_service import (
    load_paper_trading_equity_curve,
    load_paper_trading_summary,
    load_recent_paper_trades,
    paper_trading_health,
    run_paper_trading_service,
)


def _opp(**overrides):
    row = {
        "id": "opp-1",
        "strategy": "short_crypto",
        "market_id": "btc-up",
        "title": "Bitcoin Up or Down",
        "asset": "BTC",
        "side": "up",
        "entry_price": 0.5,
        "target_price": None,
        "estimated_roi": 0.5,
        "ranking_score": 90,
    }
    row.update(overrides)
    return row


def test_service_cycle_runs_engine_and_logs(tmp_path, monkeypatch):
    db_path = tmp_path / "paper_trading.db"
    log_path = tmp_path / "paper_trading.log"

    def fake_engine(db_path, config):
        assert isinstance(config, PaperTradingConfig)
        return {
            "run_id": 7,
            "opportunities_seen": 3,
            "orders_created": 1,
            "positions_opened": 1,
            "positions_settled": 0,
            "open_positions": 1,
            "equity": 100.0,
            "realized_pnl": 0.0,
            "unrealized_pnl": 0.0,
        }

    monkeypatch.setattr("src.analysis.paper_trading_service.run_paper_trading_engine", fake_engine)
    monkeypatch.setattr(
        "src.analysis.paper_trading_service.paper_trading_health",
        lambda db_path: {"status": "healthy", "equity": 100.0},
    )

    result = run_paper_trading_service(db_path=db_path, log_path=log_path, limit=3)

    assert result["accepted"] is True
    assert result["result"]["positions_opened"] == 1
    log = json.loads(log_path.read_text(encoding="utf-8").strip())
    assert log["event"] == "paper_trading_service_run"
    assert log["opportunities"] == 3
    assert log["entries"] == 1


def test_service_is_safe_to_run_repeatedly(tmp_path):
    db_path = tmp_path / "paper_trading.db"
    log_path = tmp_path / "paper_trading.log"
    calls = {"count": 0}

    def collector():
        calls["count"] += 1
        return [_opp()]

    monkeypatch_engine = paper_trading_service.run_paper_trading_engine
    try:
        paper_trading_service.run_paper_trading_engine = lambda db_path, config: run_paper_trading_engine(
            db_path=db_path,
            config=config,
            collectors=[collector],
        )
        first = run_paper_trading_service(db_path=db_path, log_path=log_path)
        second = run_paper_trading_service(db_path=db_path, log_path=log_path)
    finally:
        paper_trading_service.run_paper_trading_engine = monkeypatch_engine

    assert first["result"]["positions_opened"] == 1
    assert second["result"]["positions_opened"] == 0
    assert second["result"]["open_positions"] == 1


def test_health_report(tmp_path):
    db_path = tmp_path / "paper_trading.db"
    run_paper_trading_engine(db_path=db_path, collectors=[lambda: [_opp()]])

    health = paper_trading_health(db_path=db_path)

    assert health["last_run"]
    assert health["runs_24h"] == 1
    assert health["positions_open"] == 1
    assert health["positions_closed"] == 0
    assert health["equity"] == 100.0
    assert health["status"] == "healthy"


def test_dashboard_helpers(tmp_path):
    db_path = tmp_path / "paper_trading.db"
    run_paper_trading_engine(db_path=db_path, collectors=[lambda: [_opp(target_price=None)]])
    settle_open_positions(db_path=db_path, run_id=2, prices={"opp-1": 0.75})
    run_paper_trading_engine(db_path=db_path, collectors=[lambda: []])

    summary = load_paper_trading_summary(db_path)
    curve = load_paper_trading_equity_curve(db_path)
    trades = load_recent_paper_trades(db_path, limit=5)

    assert summary["health"]["positions_closed"] == 1
    assert curve
    assert trades[0]["opportunity_id"] == "opp-1"
    assert trades[0]["status"] == "closed"


def test_service_cli_json_output(capsys, monkeypatch, tmp_path):
    from src.cli import main

    expected = {"accepted": True, "result": {"run_id": 1}}

    def fake_service(db_path, log_path, limit):
        assert limit == 9
        return expected

    monkeypatch.setattr("src.cli.run_paper_trading_service", fake_service)
    sys.argv = ["polylens", "paper-trading-service", "--limit", "9", "--db-path", str(tmp_path / "paper.db"), "--json"]
    main()
    assert json.loads(capsys.readouterr().out) == expected


def test_health_cli_json_output(capsys, monkeypatch, tmp_path):
    from src.cli import main

    expected = {"status": "healthy", "equity": 101.0}
    monkeypatch.setattr("src.cli.build_paper_trading_health", lambda db_path: expected)
    sys.argv = ["polylens", "paper-trading-health", "--db-path", str(tmp_path / "paper.db"), "--json"]
    main()
    assert json.loads(capsys.readouterr().out) == expected


def test_systemd_units_are_oneshot_and_five_minute_timer():
    service = Path("deploy/systemd/polylens-paper-trading.service").read_text(encoding="utf-8")
    timer = Path("deploy/systemd/polylens-paper-trading.timer").read_text(encoding="utf-8")

    assert "Type=oneshot" in service
    assert "paper-trading-service --json" in service
    assert "POLYLENS_LIVE_TRADING=false" in service
    assert "POLYLENS_KALSHI_LIVE_SENDS_ENABLED=false" in service
    assert "POLYLENS_POLYMARKET_LIVE_SENDS_ENABLED=false" in service
    assert "OnUnitActiveSec=5min" in timer
    assert "Persistent=true" in timer


def test_no_live_execution_imports_order_calls_or_credentials():
    source = inspect.getsource(paper_trading_service)
    forbidden = [
        "KalshiExecutor",
        "submit_order",
        "place_order",
        "send_order",
        "private_key",
        "api_secret",
        "POLYMARKET_PRIVATE_KEY",
        "KALSHI_PRIVATE_KEY",
        "src.trading.executor",
    ]
    assert all(token not in source for token in forbidden)
