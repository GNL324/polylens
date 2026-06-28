from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.analysis.decision_intelligence import (
    build_decision_intelligence_report,
    explain_opportunity,
    format_decision_detail_telegram,
    format_decision_intelligence_telegram,
    parse_decision_detail_callback,
)
from src.analysis.opportunity_scoring_v3 import PortfolioContext, ScoringV3Weights, score_opportunity_v3
from src.analysis.paper_execution_simulator import ExecutionResult, PaperExecutionConfig, simulate_entry
from src.analysis.paper_trading_engine import init_paper_trading_db

NOW = datetime(2026, 6, 26, 12, 0, 0, tzinfo=timezone.utc)
WEIGHTS = ScoringV3Weights(
    wallet_performance=0.10,
    wallet_consistency=0.08,
    wallet_momentum=0.07,
    signal_family_accuracy=0.10,
    market_liquidity=0.10,
    bid_ask_spread=0.08,
    time_to_expiry=0.07,
    validating_wallets=0.06,
    consensus_strength=0.05,
    contrarian_bonus=0.04,
    similar_market_edge=0.10,
    execution_quality_penalty=0.08,
    portfolio_exposure_penalty=0.07,
)


def _candidate(**overrides):
    row = {
        "opportunity_id": "opp-a",
        "id": "opp-a",
        "rank": 1,
        "title": "BTC above 100k",
        "strategy": "wallet_signal",
        "market_id": "btc-100k",
        "asset": "BTC",
        "side": "yes",
        "entry_price": 0.5,
        "estimated_edge": 0.08,
        "estimated_roi": 0.08,
        "confidence": 0.82,
        "alpha_score": 88,
        "watch_score": 85,
        "wallet_roi": 0.12,
        "wallet_win_rate": 0.6,
        "validation_count": 12,
        "historical_accuracy": 0.62,
        "gate_status": "proven",
        "signal_type": "conviction",
        "signal_family": "conviction",
        "liquidity": 5000,
        "yes_bid": 0.48,
        "yes_ask": 0.52,
        "spread": 0.04,
        "quote_timestamp": "2026-06-26T11:58:00Z",
        "close_time": "2026-06-28T12:00:00Z",
        "eligible_for_paper_trading": True,
    }
    row.update(overrides)
    return row


def _scored(**overrides):
    row = _candidate(**overrides)
    scored = score_opportunity_v3(row, weights=WEIGHTS, now=NOW)
    scored["rank"] = overrides.get("rank", 1)
    scored["scoring_v3"] = {
        "final_score": scored["final_score"],
        "rank": scored["rank"],
        "components": scored["components"],
        "expected_edge": scored["expected_edge"],
        "confidence": scored["confidence"],
        "expected_execution_quality": scored["expected_execution_quality"],
        "projected_risk": scored["projected_risk"],
        "explanation_summary": scored["explanation_summary"],
    }
    return scored


def test_buy_decision_for_strong_opportunity():
    explanation = explain_opportunity(
        _scored(),
        requested_stake=2.0,
        execution=ExecutionResult(
            status="filled",
            fill_price=0.52,
            filled_stake=2.0,
            requested_stake=2.0,
            shares=3.84615385,
            slippage=0.0,
            spread_paid=0.04,
            quoted_mid=0.5,
        ),
        now=NOW,
    )

    assert explanation["decision"] == "BUY"
    assert explanation["positives"]
    assert explanation["summary"]
    assert explanation["paper_only"] is True
    assert explanation["read_only"] is True


def test_watch_decision_for_borderline_opportunity():
    explanation = explain_opportunity(
        _scored(confidence=0.5, spread=0.06, alpha_score=60, validation_count=2),
        requested_stake=2.0,
        execution=ExecutionResult(
            status="filled",
            fill_price=0.53,
            filled_stake=2.0,
            requested_stake=2.0,
            shares=3.77,
            slippage=0.0,
            spread_paid=0.06,
            quoted_mid=0.5,
        ),
        now=NOW,
    )

    assert explanation["decision"] in {"WATCH", "BLOCK"}
    assert explanation["risks"]
    assert explanation["improvement_suggestions"]


def test_block_decision_for_execution_rejection():
    explanation = explain_opportunity(
        _scored(),
        requested_stake=2.0,
        execution=ExecutionResult(
            status="rejected",
            fill_price=None,
            filled_stake=0.0,
            requested_stake=2.0,
            shares=0.0,
            slippage=0.0,
            spread_paid=0.0,
            quoted_mid=0.5,
            reason="liquidity_cap_exceeded",
        ),
        now=NOW,
    )

    assert explanation["decision"] == "BLOCK"
    assert "Liquidity cap exceeded" in explanation["blocking_reasons"]
    assert explanation["execution_prevented"]


def test_block_for_stale_market():
    config = PaperExecutionConfig(max_market_data_age_seconds=60.0, now=NOW)
    execution = simulate_entry(
        row=_candidate(quote_timestamp="2026-06-26T10:00:00Z"),
        side="yes",
        requested_stake=2.0,
        config=config,
    )
    explanation = explain_opportunity(_scored(quote_timestamp="2026-06-26T10:00:00Z"), execution=execution, requested_stake=2.0, now=NOW)

    assert execution.status == "rejected"
    assert explanation["decision"] == "BLOCK"
    assert "Market stale" in explanation["blocking_reasons"]


def test_block_for_portfolio_limit(tmp_path):
    db_path = tmp_path / "paper.db"
    init_paper_trading_db(db_path)
    portfolio = PortfolioContext(
        open_positions=[
            {
                "strategy": "wallet_signal",
                "market_id": "btc-100k",
                "asset": "BTC",
                "notional": 95.0,
                "source_wallet": "0x" + "a" * 40,
            }
        ],
        starting_bankroll=100.0,
        available_cash=2.0,
        daily_risk_used=95.0,
        daily_risk_budget=10.0,
    )
    explanation = explain_opportunity(
        _scored(),
        portfolio=portfolio,
        requested_stake=0.0,
        db_path=db_path,
        now=NOW,
    )

    assert explanation["decision"] == "BLOCK"
    assert explanation["blocking_reasons"]
    assert any("Kelly" in item or "risk" in item.lower() or "exposure" in item.lower() for item in explanation["blocking_reasons"])


def test_concentration_penalty_surfaces_in_risks():
    portfolio = PortfolioContext(
        open_positions=[
            {"strategy": "wallet_signal", "market_id": "other", "asset": "BTC", "notional": 18.0},
            {"strategy": "wallet_signal", "market_id": "other-2", "asset": "BTC", "notional": 12.0},
        ],
        starting_bankroll=100.0,
        available_cash=70.0,
        daily_risk_used=30.0,
        daily_risk_budget=10.0,
    )
    explanation = explain_opportunity(
        _scored(),
        portfolio=portfolio,
        requested_stake=2.0,
        execution=ExecutionResult(
            status="filled",
            fill_price=0.52,
            filled_stake=2.0,
            requested_stake=2.0,
            shares=3.84,
            slippage=0.0,
            spread_paid=0.04,
            quoted_mid=0.5,
        ),
        now=NOW,
    )

    assert any("exposure" in risk.lower() or "correlated" in risk.lower() for risk in explanation["risks"])


def test_explanation_formatting_fields():
    explanation = explain_opportunity(
        _scored(rank=2),
        requested_stake=2.0,
        execution=ExecutionResult(
            status="filled",
            fill_price=0.52,
            filled_stake=2.0,
            requested_stake=2.0,
            shares=3.84,
            slippage=0.0,
            spread_paid=0.04,
            quoted_mid=0.5,
        ),
        now=NOW,
    )

    assert set(explanation) >= {
        "summary",
        "positives",
        "risks",
        "decision",
        "decision_reasons",
        "blocking_reasons",
        "confidence_reducers",
        "execution_prevented",
        "improvement_suggestions",
    }
    assert "#2" in explanation["summary"] or "Ranked #2" in explanation["summary"]


def test_telegram_rendering():
    explanations = [
        explain_opportunity(
            _scored(rank=1),
            requested_stake=2.0,
            execution=ExecutionResult(
                status="filled",
                fill_price=0.52,
                filled_stake=2.0,
                requested_stake=2.0,
                shares=3.84,
                slippage=0.0,
                spread_paid=0.04,
                quoted_mid=0.5,
            ),
            now=NOW,
        )
    ]
    listing = format_decision_intelligence_telegram({"explanations": explanations})
    detail = format_decision_detail_telegram(explanations[0])

    assert "Decision Intelligence" in listing
    assert "BUY" in listing or "WATCH" in listing or "BLOCK" in listing
    assert "Positives:" in detail
    assert "Decision reasons:" in detail


def test_cli_report_shape(monkeypatch, tmp_path):
    db_path = tmp_path / "paper.db"
    init_paper_trading_db(db_path)
    monkeypatch.setattr(
        "src.analysis.opportunity_feed.get_paper_trading_opportunities",
        lambda **_kwargs: [_scored()],
    )
    monkeypatch.setattr(
        "src.analysis.outcome_validator.outcome_validation_report",
        lambda *_args, **_kwargs: {"calibration_drift": 0.0, "by_signal_family": {}},
    )
    report = build_decision_intelligence_report(db_path, now=NOW)

    assert report["paper_only"] is True
    assert report["read_only"] is True
    assert report["count"] == 1
    assert report["explanations"][0]["decision"] in {"BUY", "WATCH", "BLOCK"}


def test_decision_detail_callback_roundtrip():
    assert parse_decision_detail_callback("di_r03") == 3
    assert parse_decision_detail_callback("nav_home") is None


def test_telegram_console_decision_page(tmp_path, monkeypatch):
    from src.integrations.telegram_console import TelegramConsole, TelegramConsoleConfig

    monkeypatch.setattr(
        "src.analysis.opportunity_feed.get_paper_trading_opportunities",
        lambda **_kwargs: [_scored()],
    )
    monkeypatch.setattr(
        "src.analysis.decision_intelligence.build_decision_intelligence_report",
        lambda *_args, **_kwargs: {
            "explanations": [
                explain_opportunity(
                    _scored(),
                    requested_stake=2.0,
                    execution=ExecutionResult(
                        status="filled",
                        fill_price=0.52,
                        filled_stake=2.0,
                        requested_stake=2.0,
                        shares=3.84,
                        slippage=0.0,
                        spread_paid=0.04,
                        quoted_mid=0.5,
                    ),
                    now=NOW,
                )
            ]
        },
    )

    config = TelegramConsoleConfig(
        bot_token="test-token",
        admin_user_ids=[123],
        audit_db_path=str(tmp_path / "audit.db"),
        paper_db_path=str(tmp_path / "paper.db"),
    )
    console = TelegramConsole(
        config,
        paper_provider=lambda: {"read_only": True, "paper_only": True, "db_path": str(tmp_path / "paper.db")},
    )
    response = console.handle_console_callback(456, 123, "quick_decision_intelligence")

    assert "Decision Intelligence" in response.text
    assert response.reply_markup is not None
