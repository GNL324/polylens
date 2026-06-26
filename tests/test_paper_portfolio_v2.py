from __future__ import annotations

from datetime import datetime, timezone

from src.analysis.paper_portfolio import position_transitions, transition_position
from src.analysis.paper_trading_engine import (
    PaperTradingConfig,
    calculate_position_size,
    init_paper_trading_db,
    performance_report,
    record_equity_snapshot,
    run_paper_trading_engine,
    settle_open_positions,
)
from src.integrations.telegram_notifications import portfolio_analytics_report_text
from src.integrations.telegram_console import TelegramConsole, TelegramConsoleConfig
from src.web.mission_control_data import mission_control_snapshot


def _opportunity(identifier: str, **overrides: object) -> dict:
    row = {
        "id": identifier,
        "strategy": "wallet_copy",
        "market_id": identifier,
        "title": f"Market {identifier}",
        "asset": "BTC",
        "category": "crypto",
        "side": "yes",
        "entry_price": 0.5,
        "target_price": None,
        "estimated_roi": 1.0,
        "ranking_score": 100,
        "signal_family": "wallet_signal",
        "source_wallet": "0xabc",
        "confidence": 0.6,
        "validation_score": 0.8,
        "liquidity": 1234,
        "time_to_expiry_seconds": 3600,
    }
    row.update(overrides)
    return row


def test_configurable_position_sizing_is_deterministic(tmp_path):
    db_path = tmp_path / "paper.db"
    init_paper_trading_db(db_path)
    assert calculate_position_size(
        db_path=db_path,
        strategy="fixed",
        config=PaperTradingConfig(sizing_mode="fixed_dollar", fixed_dollar_size=7, max_strategy_exposure=1),
    ) == 7
    assert calculate_position_size(
        db_path=db_path,
        strategy="percent",
        config=PaperTradingConfig(sizing_mode="percent_equity", percent_of_equity=0.1, max_strategy_exposure=1),
    ) == 10
    result = run_paper_trading_engine(
        db_path=db_path,
        collectors=[lambda: [_opportunity("kelly")]],
        config=PaperTradingConfig(sizing_mode="kelly", kelly_fraction=0.5, kelly_cap=0.25, max_strategy_exposure=1, max_exposure_per_market=1, max_exposure_per_category=1),
    )
    assert result["positions_opened"] == 1
    assert performance_report(db_path)["open_position_value"] == 10


def test_paper_risk_controls_load_from_environment(monkeypatch):
    monkeypatch.setenv("POLYLENS_PAPER_SIZING_MODE", "fixed_dollar")
    monkeypatch.setenv("POLYLENS_PAPER_FIXED_DOLLAR_SIZE", "12.5")
    monkeypatch.setenv("POLYLENS_PAPER_DAILY_TRADE_LIMIT", "3")
    config = PaperTradingConfig.from_env()
    assert config.sizing_mode == "fixed_dollar"
    assert config.fixed_dollar_size == 12.5
    assert config.daily_trade_limit == 3


def test_market_category_and_daily_trade_limits_block_without_spending_cash(tmp_path):
    db_path = tmp_path / "paper.db"
    opportunities = [_opportunity("market-a"), _opportunity("market-b")]
    result = run_paper_trading_engine(
        db_path=db_path,
        collectors=[lambda: opportunities],
        config=PaperTradingConfig(
            sizing_mode="fixed_dollar", fixed_dollar_size=10, max_strategy_exposure=1,
            max_exposure_per_market=1, max_exposure_per_category=0.15, daily_trade_limit=1,
        ),
    )
    report = performance_report(db_path)
    assert result["positions_opened"] == 1
    assert report["cash_balance"] == 90
    assert report["exposure"] == 10


def test_daily_loss_limit_blocks_new_entries(tmp_path):
    db_path = tmp_path / "paper.db"
    config = PaperTradingConfig(sizing_mode="fixed_dollar", fixed_dollar_size=10, max_strategy_exposure=1, max_exposure_per_market=1, max_exposure_per_category=1, daily_loss_limit=1)
    run_paper_trading_engine(db_path=db_path, collectors=[lambda: [_opportunity("loss")]], config=config)
    settle_open_positions(db_path=db_path, prices={"loss": 0.4})
    result = run_paper_trading_engine(db_path=db_path, collectors=[lambda: [_opportunity("blocked-after-loss")]], config=config)
    assert result["positions_opened"] == 0
    assert any(row["status"] == "blocked" and "daily loss" in str(row["reason"]) for row in __import__("src.analysis.paper_portfolio", fromlist=["portfolio_trade_log"]).portfolio_trade_log(db_path, limit=10))


def test_lifecycle_transition_records_partial_and_close_timestamps(tmp_path):
    db_path = tmp_path / "paper.db"
    run_paper_trading_engine(db_path=db_path, collectors=[lambda: [_opportunity("lifecycle")]])
    partial = transition_position(db_path, paper_position_id=1, status="partially_exited", quantity=2, price=0.75, timestamp="2026-01-01T01:00:00Z")
    closed = transition_position(db_path, paper_position_id=1, status="closed", quantity=partial["shares"], price=0.25, timestamp="2026-01-01T02:00:00Z")
    transitions = position_transitions(db_path, 1)
    assert partial["status"] == "partially_exited"
    assert closed["status"] == "closed"
    assert [row["to_status"] for row in transitions] == ["pending", "open", "partially_exited", "closed"]


def test_equity_drawdown_statistics_and_attribution(tmp_path):
    db_path = tmp_path / "paper.db"
    opportunities = [_opportunity("winner", strategy="alpha", source_wallet="0xgood"), _opportunity("loser", strategy="beta", source_wallet="0xbad", category="politics")]
    run_paper_trading_engine(db_path=db_path, collectors=[lambda: opportunities])
    settle_open_positions(db_path=db_path, prices={"winner": 0.75, "loser": 0.25})
    record_equity_snapshot(db_path, run_id=2, timestamp="2026-01-01T00:00:00Z")
    record_equity_snapshot(db_path, run_id=3, timestamp="2026-01-02T00:00:00Z")
    report = performance_report(db_path)
    assert report["equity"] == 100
    assert report["max_drawdown"] <= 0
    assert report["profit_factor"] == 1
    assert report["average_winner"] == 1
    assert report["average_loser"] == -1
    assert report["by_strategy"]["alpha"]["closed_positions"] == 1
    assert report["by_signal_family"]["wallet_signal"]["trade_count"] == 2
    assert report["by_source_wallet"]["0xgood"]["total_pnl"] == 1
    assert report["by_market_category"]["politics"]["total_pnl"] == -1


def test_mission_control_and_telegram_render_portfolio_analytics(tmp_path, monkeypatch):
    db_path = tmp_path / "paper.db"
    run_paper_trading_engine(db_path=db_path, collectors=[lambda: [_opportunity("render")]])
    report = performance_report(db_path)
    text = portfolio_analytics_report_text({**report, "current_equity": report["equity"], "recent_trades": []})
    monkeypatch.setattr("src.web.mission_control_data._legacy_mission_control_snapshot", lambda **_kwargs: {"generated_at": "2026-01-01T00:00:00Z", "header": {}, "kpis": {}})
    snapshot = mission_control_snapshot(
        portfolio_db_path=db_path, paper_db_path=tmp_path / "short.db", polylens_db_path=tmp_path / "poly.db",
        now=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    assert "📊 Portfolio Analytics" in text
    assert "Current equity" in text
    assert snapshot["paper_portfolio"]["cash"] == 98
    assert snapshot["paper_portfolio"]["equity_curve"]["points"]
    console = TelegramConsole(
        TelegramConsoleConfig(bot_token="token", admin_user_ids=frozenset({1}), audit_db_path=str(tmp_path / "audit.db")),
        health_provider=lambda: {"status": "healthy"}, signals_provider=lambda: {}, paper_provider=lambda: {"status": "healthy"},
        paper_intelligence_provider=lambda: {**report, "current_equity": report["equity"], "recent_trades": []}, risk_provider=lambda: {"halted": False},
        now_provider=lambda: datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    response = console.handle_console_callback(1, 1, "nav_portfolio")
    assert "Portfolio Analytics" in response.text
