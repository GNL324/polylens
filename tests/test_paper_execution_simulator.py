from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.analysis.paper_execution_analytics import execution_quality_report
from src.analysis.paper_execution_simulator import (
    PaperExecutionConfig,
    parse_market_quote,
    simulate_entry,
    simulate_exit,
)
from src.analysis.paper_portfolio import portfolio_report, portfolio_trade_log
from src.analysis.paper_trading_engine import (
    PaperTradingConfig,
    init_paper_trading_db,
    performance_report,
    run_paper_trading_engine,
    settle_open_positions,
)

NOW = datetime(2026, 6, 26, 12, 0, 0, tzinfo=timezone.utc)


def _config(**overrides) -> PaperExecutionConfig:
    base = {
        "max_market_data_age_seconds": 300.0,
        "default_spread": 0.02,
        "default_slippage_bps": 10.0,
        "liquidity_participation_cap": 0.25,
        "allow_partial_fills": True,
        "now": NOW,
    }
    base.update(overrides)
    return PaperExecutionConfig(**base)


def _market(**overrides) -> dict:
    row = {
        "id": "m1",
        "strategy": "paper_copy_trader",
        "market_id": "m1",
        "title": "BTC Up",
        "asset": "BTC",
        "side": "yes",
        "entry_price": 0.5,
        "ranking_score": 99,
        "yes_bid": 0.48,
        "yes_ask": 0.52,
        "liquidity": 100.0,
        "market_status": "open",
        "quote_timestamp": "2026-06-26T11:59:30Z",
    }
    row.update(overrides)
    return row


def test_buy_fills_near_ask():
    fill = simulate_entry(row=_market(), side="yes", requested_stake=2.0, config=_config(default_slippage_bps=0.0))

    assert fill.status == "filled"
    assert fill.fill_price == pytest.approx(0.52)
    assert fill.filled_stake == 2.0


def test_buy_no_uses_no_ask_or_one_minus_bid():
    fill = simulate_entry(
        row=_market(side="no", yes_bid=0.48, yes_ask=0.52, no_ask=0.48),
        side="no",
        requested_stake=2.0,
        config=_config(default_slippage_bps=0.0),
    )

    assert fill.status == "filled"
    assert fill.fill_price == pytest.approx(0.48)

    derived = simulate_entry(
        row=_market(side="no", yes_bid=0.48, yes_ask=0.52),
        side="no",
        requested_stake=2.0,
        config=_config(default_slippage_bps=0.0),
    )
    assert derived.fill_price == pytest.approx(0.52)


def test_sell_exits_near_bid():
    exit_fill = simulate_exit(
        row=_market(yes_bid=0.47, yes_ask=0.53, entry_price=0.52),
        side="yes",
        shares=4.0,
        config=_config(default_slippage_bps=0.0),
    )

    assert exit_fill.status == "filled"
    assert exit_fill.fill_price == pytest.approx(0.47)


def test_slippage_applied_on_buy():
    fill = simulate_entry(row=_market(), side="yes", requested_stake=2.0, config=_config(default_slippage_bps=50.0))

    assert fill.fill_price == pytest.approx(0.5226)


def test_stale_market_data_rejected():
    fill = simulate_entry(
        row=_market(quote_timestamp="2026-06-26T11:00:00Z"),
        side="yes",
        requested_stake=2.0,
        config=_config(max_market_data_age_seconds=120.0),
    )

    assert fill.status == "rejected"
    assert fill.reason == "stale_market_data"


def test_closed_market_rejected():
    fill = simulate_entry(
        row=_market(market_status="closed"),
        side="yes",
        requested_stake=2.0,
        config=_config(),
    )

    assert fill.status == "rejected"
    assert fill.reason == "market_closed"


def test_expired_market_rejected():
    fill = simulate_entry(
        row=_market(expiry_time="2026-06-26T11:30:00Z"),
        side="yes",
        requested_stake=2.0,
        config=_config(),
    )

    assert fill.status == "rejected"
    assert fill.reason == "market_expired"


def test_partial_fill_when_liquidity_cap_exceeded():
    fill = simulate_entry(
        row=_market(liquidity=10.0),
        side="yes",
        requested_stake=5.0,
        config=_config(liquidity_participation_cap=0.25, allow_partial_fills=True),
    )

    assert fill.status == "partial"
    assert fill.filled_stake == pytest.approx(2.5)
    assert fill.reason == "partial_fill_liquidity_cap"


def test_liquidity_cap_rejects_when_partial_disabled():
    fill = simulate_entry(
        row=_market(liquidity=10.0),
        side="yes",
        requested_stake=5.0,
        config=_config(liquidity_participation_cap=0.25, allow_partial_fills=False),
    )

    assert fill.status == "rejected"
    assert fill.reason == "liquidity_cap_exceeded"


def test_engine_creates_blocked_record_on_stale_data(tmp_path):
    db_path = tmp_path / "paper.db"
    stale = _market(id="stale", quote_timestamp="2026-06-26T10:00:00Z")

    run_paper_trading_engine(
        db_path=db_path,
        collectors=[lambda: [stale]],
        config=PaperTradingConfig(execution=_config(max_market_data_age_seconds=120.0)),
    )

    log = portfolio_trade_log(db_path, limit=10)
    assert any(row["status"] == "blocked" and row["reason"] == "stale_market_data" for row in log)


def test_partial_fill_preserves_bankroll_accounting(tmp_path):
    db_path = tmp_path / "paper.db"
    market = _market(id="partial", liquidity=8.0)

    run_paper_trading_engine(
        db_path=db_path,
        collectors=[lambda: [market]],
        config=PaperTradingConfig(
            risk_per_trade=0.05,
            execution=_config(liquidity_participation_cap=0.25, allow_partial_fills=True, default_slippage_bps=0.0),
        ),
    )

    report = portfolio_report(db_path)
    assert report["cash_balance"] == pytest.approx(100 - 2.0)
    assert report["total_equity"] == pytest.approx(100.0)


def test_execution_metrics_aggregate(tmp_path):
    db_path = tmp_path / "paper.db"
    good = _market(id="good")
    stale = _market(id="stale", quote_timestamp="2026-06-26T10:00:00Z")

    run_paper_trading_engine(
        db_path=db_path,
        collectors=[lambda: [good, stale]],
        config=PaperTradingConfig(execution=_config(max_market_data_age_seconds=120.0, default_slippage_bps=10.0)),
    )

    metrics = execution_quality_report(db_path)
    assert metrics["filled_orders"] == 1
    assert metrics["blocked_orders"] == 1
    assert metrics["fill_rate"] == pytest.approx(0.5)
    assert metrics["average_slippage"] > 0
    assert metrics["top_rejection_reasons"][0]["reason"] == "stale_market_data"


def test_exit_uses_simulated_bid_price(tmp_path):
    db_path = tmp_path / "paper.db"
    market = _market(id="exit", yes_bid=0.48, yes_ask=0.52)

    run_paper_trading_engine(
        db_path=db_path,
        collectors=[lambda: [market]],
        config=PaperTradingConfig(execution=_config(default_slippage_bps=0.0)),
    )
    settle_open_positions(
        db_path=db_path,
        prices={"exit": 0.48},
        execution=_config(default_slippage_bps=0.0),
    )

    report = performance_report(db_path)
    assert report["closed_positions"] == 1
    assert report["realized_pnl"] == pytest.approx(-0.153846)


def test_missing_metadata_uses_defaults_and_notes():
    quote = parse_market_quote({"entry_price": 0.5}, config=_config())

    assert quote.best_ask_yes == pytest.approx(0.51)
    assert any("missing" in note for note in quote.metadata_notes)
