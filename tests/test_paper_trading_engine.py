from __future__ import annotations

import inspect
import json
import sys

from src.analysis import paper_trading_engine
from src.analysis.paper_trading_engine import (
    PaperTradingConfig,
    calculate_position_size,
    equity_report,
    init_paper_trading_db,
    open_positions_report,
    performance_report,
    record_equity_snapshot,
    run_paper_trading_engine,
    settle_open_positions,
)
from src.sqlite_utils import closing_connection


def _opp(**overrides):
    row = {
        "id": "opp-1",
        "strategy": "short_crypto",
        "market_id": "btc-up",
        "title": "Bitcoin Up or Down",
        "asset": "BTC",
        "side": "up",
        "entry_price": 0.5,
        "target_price": 0.75,
        "estimated_roi": 0.5,
        "ranking_score": 90,
    }
    row.update(overrides)
    return row


def test_position_creation(tmp_path):
    db_path = tmp_path / "paper_trading.db"

    result = run_paper_trading_engine(db_path=db_path, collectors=[lambda: [_opp()]])

    assert result["positions_opened"] == 1
    assert result["orders_created"] == 1
    positions = open_positions_report(db_path)
    assert positions["count"] == 1
    assert positions["open_positions"][0]["notional"] == 2
    assert positions["open_positions"][0]["shares"] == 4


def test_settlement_records_pnl(tmp_path):
    db_path = tmp_path / "paper_trading.db"
    run_paper_trading_engine(db_path=db_path, collectors=[lambda: [_opp(target_price=None)]])

    settled = settle_open_positions(db_path=db_path, run_id=2, prices={"opp-1": 0.75})

    report = performance_report(db_path)
    assert settled == 1
    assert report["closed_positions"] == 1
    assert report["realized_pnl"] == 1
    assert report["roi"] == 0.01


def test_bankroll_sizing(tmp_path):
    db_path = tmp_path / "paper_trading.db"
    init_paper_trading_db(db_path)

    size = calculate_position_size(db_path=db_path, strategy="short_crypto", config=PaperTradingConfig())

    assert size == 2


def test_exposure_limits(tmp_path):
    db_path = tmp_path / "paper_trading.db"
    opportunities = [_opp(id=f"opp-{i}", market_id=f"m-{i}", target_price=None, ranking_score=100 - i) for i in range(12)]

    result = run_paper_trading_engine(db_path=db_path, collectors=[lambda: opportunities])

    assert result["positions_opened"] == 10
    assert result["open_positions"] == 10


def test_strategy_exposure_limit_blocks_extra_positions(tmp_path):
    db_path = tmp_path / "paper_trading.db"
    opportunities = [_opp(id=f"opp-{i}", market_id=f"m-{i}", target_price=None, strategy="same") for i in range(11)]

    result = run_paper_trading_engine(db_path=db_path, collectors=[lambda: opportunities], config=PaperTradingConfig(max_open_positions=20))

    assert result["positions_opened"] == 10
    assert result["open_positions"] == 10


def test_equity_curve_generation_and_drawdown(tmp_path):
    db_path = tmp_path / "paper_trading.db"
    run_paper_trading_engine(db_path=db_path, collectors=[lambda: [_opp(target_price=None)]])
    settle_open_positions(db_path=db_path, run_id=2, prices={"opp-1": 0.25})
    record_equity_snapshot(db_path, run_id=2)

    report = equity_report(db_path)

    assert len(report["equity_curve"]) == 2
    assert report["latest_equity"] == 99
    assert report["max_drawdown"] == -1


def test_performance_metrics_profit_factor_and_expectancy(tmp_path):
    db_path = tmp_path / "paper_trading.db"
    run_paper_trading_engine(
        db_path=db_path,
        collectors=[lambda: [_opp(id="win", market_id="win", target_price=None), _opp(id="loss", market_id="loss", target_price=None)]],
    )
    settle_open_positions(db_path=db_path, run_id=2, prices={"win": 0.75, "loss": 0.25})
    record_equity_snapshot(db_path, run_id=2)

    report = performance_report(db_path)

    assert report["closed_positions"] == 2
    assert report["realized_pnl"] == 0
    assert report["win_rate"] == 0.5
    assert report["expectancy"] == 0
    assert report["profit_factor"] == 1
    assert report["by_asset"]["BTC"]["closed_positions"] == 2


def test_cli_output(capsys, monkeypatch, tmp_path):
    from src.cli import main

    db_path = tmp_path / "paper_trading.db"
    expected = {"run_id": 1, "positions_opened": 1}

    def fake_run(db_path, config):
        return expected

    monkeypatch.setattr("src.cli.run_paper_trading_engine", fake_run)
    sys.argv = ["polylens", "paper-trading-engine", "--run", "--db-path", str(db_path), "--json"]
    main()
    output = json.loads(capsys.readouterr().out)
    assert output == expected


def test_report_cli_output(capsys, monkeypatch, tmp_path):
    from src.cli import main

    expected = {"realized_pnl": 1.25}

    def fake_report(db_path):
        return expected

    monkeypatch.setattr("src.cli.build_paper_performance_report", fake_report)
    sys.argv = ["polylens", "paper-performance-report", "--db-path", str(tmp_path / "paper.db"), "--json"]
    main()
    output = json.loads(capsys.readouterr().out)
    assert output == expected


def test_no_live_execution_imports_or_calls():
    source = inspect.getsource(paper_trading_engine)
    forbidden = [
        "KalshiExecutor",
        "submit_order",
        "place_order",
        "send_order",
        "private_key",
        "api_secret",
        "src.trading.executor",
    ]
    assert all(token not in source for token in forbidden)
