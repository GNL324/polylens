from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.analysis.opportunity_scoring_v3 import (
    PortfolioContext,
    ScoringV3Weights,
    format_opportunity_ranking_text,
    load_portfolio_context,
    rank_opportunities_v3,
    score_opportunity_v3,
)
from src.analysis.paper_trading_engine import init_paper_trading_db, run_paper_trading_engine

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


def test_score_includes_all_components():
    result = score_opportunity_v3(_candidate(), weights=WEIGHTS, now=NOW)

    assert 0 <= result["final_score"] <= 100
    assert set(result["components"]) >= {
        "wallet_performance",
        "wallet_consistency",
        "wallet_momentum",
        "signal_family_accuracy",
        "market_liquidity",
        "bid_ask_spread",
        "time_to_expiry",
        "validating_wallets",
        "consensus_strength",
        "contrarian_bonus",
        "similar_market_edge",
        "execution_quality_penalty",
        "portfolio_exposure_penalty",
    }
    for component in result["components"].values():
        assert "score" in component
        assert "weight" in component
        assert "weighted_contribution" in component
        assert component["reason"]


def test_strong_wallet_scores_higher_than_weak_wallet():
    strong = score_opportunity_v3(
        _candidate(alpha_score=95, watch_score=92, wallet_roi=0.2, wallet_win_rate=0.7),
        weights=WEIGHTS,
        now=NOW,
    )
    weak = score_opportunity_v3(
        _candidate(opportunity_id="opp-b", id="opp-b", alpha_score=20, watch_score=15, wallet_roi=-0.1, wallet_win_rate=0.2),
        weights=WEIGHTS,
        now=NOW,
    )

    assert strong["final_score"] > weak["final_score"]
    assert strong["components"]["wallet_performance"]["score"] > weak["components"]["wallet_performance"]["score"]


def test_portfolio_exposure_penalty_reduces_score():
    base = score_opportunity_v3(_candidate(), weights=WEIGHTS, now=NOW)
    portfolio = PortfolioContext(
        open_positions=[
            {
                "strategy": "wallet_signal",
                "market_id": "btc-100k",
                "asset": "BTC",
                "notional": 18.0,
                "source_wallet": "0x" + "a" * 40,
            }
        ],
        starting_bankroll=100.0,
        available_cash=20.0,
        daily_risk_used=18.0,
        daily_risk_budget=10.0,
    )
    penalized = score_opportunity_v3(
        _candidate(wallet="0x" + "a" * 40, source_wallet="0x" + "a" * 40),
        portfolio=portfolio,
        weights=WEIGHTS,
        now=NOW,
    )

    assert penalized["components"]["portfolio_exposure_penalty"]["score"] > base["components"]["portfolio_exposure_penalty"]["score"]
    assert penalized["final_score"] < base["final_score"]


def test_execution_penalty_for_stale_quote():
    fresh = score_opportunity_v3(_candidate(), weights=WEIGHTS, now=NOW)
    stale = score_opportunity_v3(
        _candidate(quote_timestamp="2026-06-26T08:00:00Z"),
        weights=WEIGHTS,
        now=NOW,
    )

    assert stale["components"]["execution_quality_penalty"]["score"] > fresh["components"]["execution_quality_penalty"]["score"]
    assert stale["final_score"] < fresh["final_score"]


def test_ranking_is_stable_and_deterministic():
    rows = [
        _candidate(opportunity_id="low", id="low", estimated_edge=0.02, alpha_score=40),
        _candidate(opportunity_id="high", id="high", estimated_edge=0.12, alpha_score=95),
        _candidate(opportunity_id="mid", id="mid", estimated_edge=0.06, alpha_score=70),
    ]
    first = rank_opportunities_v3(rows, weights=WEIGHTS, now=NOW)
    second = rank_opportunities_v3(rows, weights=WEIGHTS, now=NOW)

    assert [row["opportunity_id"] for row in first] == [row["opportunity_id"] for row in second]
    assert first[0]["opportunity_id"] == "high"
    assert first[0]["rank"] == 1
    assert first[0]["scoring_v3"]["final_score"] == first[0]["final_score"]


def test_explainability_summary_mentions_leading_factor():
    result = score_opportunity_v3(_candidate(), weights=WEIGHTS, now=NOW)

    assert "Score" in result["explanation_summary"]
    assert result["explanation_summary"]
    assert result["confidence"] > 0


def test_format_opportunity_ranking_text_includes_rank_one_explanation():
    ranked = rank_opportunities_v3([_candidate()], weights=WEIGHTS, now=NOW, limit=1)
    text = format_opportunity_ranking_text(ranked, limit=1)

    assert "🏆 Opportunity Rankings" in text
    assert "#1" in text
    assert "BTC above 100k" in text


def test_load_portfolio_context_from_paper_db(tmp_path):
    db_path = tmp_path / "paper.db"
    init_paper_trading_db(db_path)
    run_paper_trading_engine(
        db_path=db_path,
        collectors=[
            lambda: [
                {
                    "id": "held",
                    "strategy": "wallet_signal",
                    "market_id": "held",
                    "title": "Held market",
                    "asset": "BTC",
                    "side": "yes",
                    "entry_price": 0.5,
                    "ranking_score": 90,
                }
            ]
        ],
    )

    context = load_portfolio_context(db_path)

    assert len(context.open_positions) == 1
    assert context.strategy_exposure.get("wallet_signal", 0) > 0


def test_contrarian_bonus_increases_score_for_contrarian_signal():
    normal = score_opportunity_v3(_candidate(signal_type="conviction"), weights=WEIGHTS, now=NOW)
    contrarian = score_opportunity_v3(
        _candidate(opportunity_id="opp-c", id="opp-c", signal_type="contrarian", estimated_edge=0.1),
        weights=WEIGHTS,
        now=NOW,
    )

    assert contrarian["components"]["contrarian_bonus"]["score"] > normal["components"]["contrarian_bonus"]["score"]
