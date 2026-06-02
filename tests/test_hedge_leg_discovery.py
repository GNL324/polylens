from unittest.mock import patch

from src.analysis.hedge_leg_discovery import discover_hedge_leg, explain_hedge_search
from src.analysis.hedged_arbitrage import classify_candidate


def test_direct_binary_yes_no_has_full_hedge():
    candidate = {"polymarket_implied_yes_price": 0.42, "kalshi_implied_no_price": 0.53}

    result = discover_hedge_leg(candidate)

    assert result == {"hedge_leg_found": True, "coverage_percent": 100, "hedge_type": "binary_complete", "diagnostic": "full hedge"}


def test_field_contract_detected_for_sportsbook_future():
    candidate = {"market_type": "championship_winner", "sportsbook_team": "New York Knicks", "polymarket_title": "Will the New York Knicks win the 2026 NBA Finals?"}
    inventory = [{"venue": "polymarket", "title": "Any other team wins the NBA Finals", "outcome": "Field", "price": 0.88}]

    result = discover_hedge_leg(candidate, inventory)

    assert result["hedge_leg_found"] is True
    assert result["coverage_percent"] == 100
    assert result["hedge_type"] == "field_complete"


def test_knicks_future_without_no_field_reports_missing_hedge_leg():
    candidate = {"market_type": "championship_winner", "sportsbook_team": "New York Knicks", "polymarket_title": "Will the New York Knicks win the 2026 NBA Finals?"}

    result = explain_hedge_search(candidate, [])

    assert result["hedge_leg_found"] is False
    assert result["diagnostic"] == "missing hedge leg"


def test_partial_hedge_detected_when_related_market_exists():
    candidate = {"market_type": "championship_winner", "sportsbook_team": "New York Knicks", "polymarket_title": "Will the New York Knicks win the 2026 NBA Finals?"}
    inventory = [{"title": "Will the New York Knicks make the Eastern Conference Finals?"}]

    result = discover_hedge_leg(candidate, inventory)

    assert result["hedge_leg_found"] is False
    assert result["coverage_percent"] == 65
    assert result["diagnostic"] == "partial hedge"


def test_settlement_mismatch_reported():
    candidate = {"market_type": "championship_winner", "sportsbook_team": "New York Knicks", "polymarket_title": "Will the New York Knicks win the 2026 NBA Finals?"}
    inventory = [{"title": "No New York Knicks win tonight game", "price": 0.4}]

    result = discover_hedge_leg(candidate, inventory)

    assert result["diagnostic"] == "settlement mismatch"


def test_discovered_field_leg_can_upgrade_to_true_arbitrage():
    candidate = {"venue_pair": "polymarket_sportsbook", "market_type": "championship_winner", "sportsbook_implied_probability": 0.1, "sportsbook_team": "New York Knicks", "polymarket_title": "Will the New York Knicks win the 2026 NBA Finals?", "hedge_inventory": [{"venue": "polymarket", "title": "Any other team wins the NBA Finals", "outcome": "Field", "price": 0.85}]}

    result = classify_candidate(candidate)

    assert result["opportunity_type"] == "true_arbitrage"
    assert result["total_cost"] == 0.95
    assert result["guaranteed_roi"] > 0


@patch("src.cli.scan_live_arb")
def test_find_hedges_cli_smoke(mock_scan_live, capsys):
    from src.cli import find_hedges

    mock_scan_live.return_value = {"top_scored_candidates": [{"market_type": "championship_winner", "sportsbook_team": "New York Knicks", "polymarket_title": "Will the New York Knicks win the 2026 NBA Finals?"}]}

    result = find_hedges(keyword="knicks", sport_key="basketball_nba")

    assert result["diagnostic_counts"] == {"missing hedge leg": 1}
    assert "Candidates inspected: 1" in capsys.readouterr().out
