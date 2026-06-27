from __future__ import annotations

from datetime import datetime, timezone

from src.analysis.paper_intelligence import paper_trading_intelligence
from src.analysis.paper_trading_engine import (
    PaperTradingConfig,
    init_paper_trading_db,
    performance_report,
    run_paper_trading_engine,
    settle_open_positions,
)
from src.analysis.paper_portfolio import portfolio_trade_log
from src.integrations.telegram_console import TelegramConsole, TelegramConsoleConfig
from src.sqlite_utils import closing_connection
from src.web.mission_control_data import mission_control_snapshot


def _opportunity(identifier: str, *, strategy: str = "paper_copy_trader", target: float | None = None) -> dict:
    return {
        "id": identifier,
        "strategy": strategy,
        "market_id": identifier,
        "title": f"Market {identifier}",
        "asset": "BTC",
        "side": "yes",
        "entry_price": 0.5,
        "target_price": target,
        "ranking_score": 99,
        "signal_family": "wallet_signal",
        "wallet": "0x1111111111111111111111111111111111111111",
        "reason": "paper test",
    }


def test_canonical_portfolio_starts_with_100_dollars(tmp_path):
    db_path = tmp_path / "paper.db"
    init_paper_trading_db(db_path)

    report = performance_report(db_path)

    assert report["starting_bankroll"] == 100
    assert report["cash_balance"] == 100
    assert report["total_equity"] == 100
    assert report["total_pnl"] == 0


def test_open_position_reserves_cash_and_never_allows_negative_cash(tmp_path):
    db_path = tmp_path / "paper.db"
    opportunities = [_opportunity(f"trade-{index}", strategy=f"strategy-{index}") for index in range(30)]

    result = run_paper_trading_engine(
        db_path=db_path,
        collectors=[lambda: opportunities],
        config=PaperTradingConfig(max_open_positions=100, risk_per_trade=0.05, max_strategy_exposure=1.0),
    )
    report = performance_report(db_path)

    assert result["positions_opened"] == 20
    assert any(row["status"] == "blocked" for row in portfolio_trade_log(db_path, limit=100))
    assert report["cash_balance"] == 0
    assert report["open_position_value"] == 100
    assert report["total_equity"] == 100


def test_close_realizes_pnl_and_open_mark_is_unrealized_only(tmp_path):
    db_path = tmp_path / "paper.db"
    run_paper_trading_engine(db_path=db_path, collectors=[lambda: [_opportunity("open"), _opportunity("close")]])
    with closing_connection(db_path) as conn:
        conn.execute("UPDATE paper_positions SET current_price=0.6, unrealized_pnl=0.4 WHERE opportunity_id='open'")
    settle_open_positions(db_path=db_path, prices={"close": 0.75})

    report = performance_report(db_path)

    assert report["realized_pnl"] == 1
    assert report["unrealized_pnl"] == 0.4
    assert report["total_pnl"] == 1.4
    assert report["cash_balance"] == 99


def test_win_rate_counts_closed_trades_only(tmp_path):
    db_path = tmp_path / "paper.db"
    run_paper_trading_engine(db_path=db_path, collectors=[lambda: [_opportunity("closed"), _opportunity("still-open")]])
    settle_open_positions(db_path=db_path, prices={"closed": 0.75})

    report = performance_report(db_path)

    assert report["closed_positions"] == 1
    assert report["open_positions"] == 1
    assert report["win_rate"] == 1


def test_legacy_anomalous_rows_are_preserved_but_excluded(tmp_path):
    db_path = tmp_path / "paper.db"
    init_paper_trading_db(db_path)
    with closing_connection(db_path) as conn:
        conn.execute("INSERT INTO paper_runs (run_timestamp, opportunities_seen, orders_created, positions_opened, positions_settled, equity, raw_json) VALUES ('2026-01-01T00:00:00Z', 0, 0, 0, 0, 100, '{}')")
        conn.execute("INSERT INTO paper_orders (run_id, opportunity_id, strategy, side, market_id, title, asset, simulated_price, stake, status, raw_json) VALUES (1, 'legacy', 'paper_copy_trader', 'yes', 'legacy', 'Legacy', 'BTC', 0.5, 2, 'filled', '{}')")
        conn.execute("INSERT INTO paper_positions (order_id, opportunity_id, strategy, market_id, title, asset, side, entry_timestamp, entry_price, shares, notional, status, current_price, exit_timestamp, exit_price, realized_pnl, unrealized_pnl) VALUES (1, 'legacy', 'paper_copy_trader', 'legacy', 'Legacy', 'BTC', 'yes', '2026-01-01T00:00:00Z', 0.5, 4, 2, 'closed', 0.5, '2026-01-01T01:00:00Z', 1000000, 3999998, 0)")

    report = performance_report(db_path)
    intelligence = paper_trading_intelligence(db_path=db_path, short_crypto_db_path=tmp_path / "missing.db")

    assert report["legacy_anomalous_count"] == 1
    assert report["total_pnl"] == 0
    assert intelligence["total_pnl"] == 0
    assert intelligence["recent_trades"][0]["status"] == "legacy_anomalous"


def test_telegram_performance_and_trades_log_use_canonical_fields(tmp_path):
    db_path = tmp_path / "paper.db"
    run_paper_trading_engine(db_path=db_path, collectors=[lambda: [_opportunity("telegram")]])
    paper = paper_trading_intelligence(db_path=db_path, short_crypto_db_path=tmp_path / "missing.db")
    config = TelegramConsoleConfig(bot_token="token", admin_user_ids=frozenset({1}), audit_db_path=str(tmp_path / "audit.db"), paper_db_path=str(db_path))
    console = TelegramConsole(
        config,
        health_provider=lambda: {"status": "healthy"},
        signals_provider=lambda: {},
        paper_provider=lambda: {"status": "healthy"},
        paper_intelligence_provider=lambda: paper,
        risk_provider=lambda: {"halted": False},
        now_provider=lambda: datetime(2026, 1, 1, tzinfo=timezone.utc),
    )

    performance = console.handle_console_callback(1, 1, "nav_performance")
    trade_log = console.handle_console_callback(1, 1, "quick_trades_log")

    perf_text = performance.text
    trades_text = trade_log.text

    for label in (
        "Starting Bankroll",
        "Current Equity",
        "Cash",
        "Open Position Value",
        "Open Positions",
        "Realized PnL",
        "Unrealized PnL",
        "Total PnL",
        "ROI",
        "Win Rate",
        "Closed Trades",
    ):
        assert label in perf_text, f"missing canonical performance field: {label}"

    assert "+$100.00" in perf_text
    assert "+$98.00" in perf_text
    assert "🏠 Home › 📈 Performance" in perf_text

    assert "📜 Trades Log" in trades_text
    assert "paper_copy_trader" in trades_text
    assert "Market telegram" in trades_text


def test_mission_control_uses_canonical_portfolio_metrics(tmp_path, monkeypatch):
    db_path = tmp_path / "paper.db"
    run_paper_trading_engine(db_path=db_path, collectors=[lambda: [_opportunity("mission")]])
    monkeypatch.setattr(
        "src.web.mission_control_data._legacy_mission_control_snapshot",
        lambda **_kwargs: {"header": {}, "kpis": {}},
    )

    snapshot = mission_control_snapshot(
        paper_db_path=tmp_path / "short.db",
        polylens_db_path=tmp_path / "polylens.db",
        portfolio_db_path=db_path,
        now=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )

    assert snapshot["kpis"]["starting_bankroll"] == 100
    assert snapshot["kpis"]["current_equity"] == 100
    assert snapshot["kpis"]["cash_balance"] == 98
