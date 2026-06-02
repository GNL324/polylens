from unittest.mock import patch

from src.analysis.hedged_arbitrage import classify_candidate, classify_arbitrage_candidates


def test_polymarket_yes_kalshi_no_below_one_creates_true_arb():
    candidate = {"venue_pair": "polymarket_kalshi", "confidence_band": "high", "polymarket_implied_yes_price": 0.42, "kalshi_implied_no_price": 0.53}

    result = classify_candidate(candidate, bankroll=1000)

    assert result["opportunity_type"] == "true_arbitrage"
    assert result["buy_venue_a"] == "polymarket"
    assert result["buy_outcome_b"] == "NO"
    assert result["total_cost"] == 0.95
    assert result["guaranteed_profit"] == 0.05
    assert result["guaranteed_roi"] > 0
    assert result["stake_a"] > 0


def test_reverse_side_true_arb():
    candidate = {"venue_pair": "polymarket_kalshi", "confidence_band": "high", "kalshi_implied_yes_price": 0.41, "polymarket_implied_no_price": 0.54}

    result = classify_candidate(candidate)

    assert result["opportunity_type"] == "true_arbitrage"
    assert result["buy_venue_a"] == "kalshi"
    assert result["buy_venue_b"] == "polymarket"


def test_total_cost_at_or_above_one_rejected():
    candidate = {"venue_pair": "polymarket_kalshi", "confidence_band": "high", "polymarket_implied_yes_price": 0.52, "kalshi_implied_no_price": 0.53}

    result = classify_candidate(candidate)

    assert result["opportunity_type"] == "positive_ev"
    assert result["true_arbitrage_rejection_reason"] == "total cost >= 1"


def test_sportsbook_future_vs_polymarket_yes_is_positive_ev_without_hedge_leg():
    candidate = {"venue_pair": "polymarket_sportsbook", "market_type": "championship_winner", "polymarket_implied_yes_price": 0.08, "sportsbook_implied_probability": 0.1, "sportsbook_team": "New York Knicks", "polymarket_title": "Will the Knicks win NBA Finals?"}

    result = classify_candidate(candidate)

    assert result["opportunity_type"] == "positive_ev"
    assert result["true_arbitrage_rejection_reason"] == "missing NO side"


def test_sportsbook_future_plus_polymarket_no_true_arb_when_fully_covered():
    candidate = {"venue_pair": "polymarket_sportsbook", "market_type": "championship_winner", "polymarket_implied_no_price": 0.82, "sportsbook_implied_probability": 0.12, "sportsbook_team": "New York Knicks", "polymarket_title": "Will the Knicks win NBA Finals?", "outcomes_fully_covered": True}

    result = classify_candidate(candidate)

    assert result["opportunity_type"] == "true_arbitrage"
    assert result["total_cost"] == 0.94


def test_cross_market_hedge_has_residual_risk():
    candidate = {"venue_pair": "polymarket_sportsbook", "market_type": "championship_winner", "polymarket_implied_yes_price": 0.08, "sportsbook_implied_probability": 0.1, "sportsbook_team": "New York Knicks", "polymarket_title": "Will Knicks win NBA Finals?", "confidence_band": "high"}

    result = classify_candidate(candidate, include_hedges=True)

    assert result["opportunity_type"] == "cross_market_hedge"
    assert result["residual_risk"]


def test_classification_groups_positive_ev_separately():
    groups = classify_arbitrage_candidates([
        {"venue_pair": "polymarket_kalshi", "confidence_band": "high", "polymarket_implied_yes_price": 0.42, "kalshi_implied_no_price": 0.53},
        {"venue_pair": "polymarket_sportsbook", "market_type": "championship_winner", "polymarket_implied_yes_price": 0.08, "sportsbook_implied_probability": 0.1, "sportsbook_team": "New York Knicks", "polymarket_title": "Will Knicks win NBA Finals?"},
    ])

    assert len(groups["true_arbitrage_candidates"]) == 1
    assert len(groups["positive_ev_candidates"]) == 1


@patch("src.cli.scan_live_arb")
def test_scan_true_arb_cli_smoke(mock_scan_live, capsys):
    from src.cli import scan_true_arb

    mock_scan_live.return_value = {"top_scored_candidates": [{"venue_pair": "polymarket_kalshi", "confidence_band": "high", "polymarket_implied_yes_price": 0.42, "kalshi_implied_no_price": 0.53}]}

    result = scan_true_arb(keyword="knicks", sport_key="basketball_nba", bankroll=1000)

    assert len(result["true_arbitrage_candidates"]) == 1
    assert "True arbitrage candidates: 1" in capsys.readouterr().out
