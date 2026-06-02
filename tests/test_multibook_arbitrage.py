from unittest.mock import patch

from src.analysis.multibook_arbitrage import scan_multibook_arbitrage
from src.analysis.synthetic_field import build_synthetic_field


def test_two_book_moneyline_arb_detected():
    lines = [
        {"event_id": "evt", "team": "Knicks", "market_type": "game_winner", "bookmaker_name": "BookA", "implied_probability": 0.45, "odds": 122},
        {"event_id": "evt", "team": "Spurs", "market_type": "game_winner", "bookmaker_name": "BookB", "implied_probability": 0.50, "odds": 100},
    ]

    result = scan_multibook_arbitrage(lines, bankroll=1000)

    assert len(result["sportsbook_multibook_arbs"]) == 1
    arb = result["sportsbook_multibook_arbs"][0]
    assert arb["guaranteed_roi"] > 0
    assert arb["stake_a"] > 0
    assert arb["stake_b"] > 0


def test_no_arb_when_implied_probability_sum_at_or_above_one():
    lines = [
        {"event_id": "evt", "team": "Knicks", "market_type": "game_winner", "bookmaker_name": "BookA", "implied_probability": 0.55},
        {"event_id": "evt", "team": "Spurs", "market_type": "game_winner", "bookmaker_name": "BookB", "implied_probability": 0.50},
    ]

    result = scan_multibook_arbitrage(lines)

    assert result["sportsbook_multibook_arbs"] == []
    assert result["diagnostics"] == {"total implied probability >= 1": 1}


def test_futures_field_arb_detected_when_all_outcomes_covered():
    lines = [
        {"event_id": "nba", "league": "NBA", "team": "Knicks", "market_type": "championship_winner", "bookmaker_name": "BookA", "implied_probability": 0.10},
        {"event_id": "nba", "league": "NBA", "team": "Celtics", "market_type": "championship_winner", "bookmaker_name": "BookB", "implied_probability": 0.25},
        {"event_id": "nba", "league": "NBA", "team": "Lakers", "market_type": "championship_winner", "bookmaker_name": "BookB", "implied_probability": 0.60},
    ]

    result = scan_multibook_arbitrage(lines)

    assert result["synthetic_field_arbs"]
    assert result["synthetic_field_arbs"][0]["guaranteed_roi"] > 0


def test_partial_field_rejected():
    field = build_synthetic_field(
        [{"team": "Knicks", "implied_probability": 0.1}, {"team": "Celtics", "implied_probability": 0.3}],
        "Knicks",
        expected_teams=["Knicks", "Celtics", "Lakers"],
    )

    assert field["coverage_complete"] is False
    assert field["diagnostic"] == "missing team outcomes"


def test_knicks_case_reports_incomplete_field_or_no_opposite_side():
    lines = [{"event_id": "nba", "league": "NBA", "team": "Knicks", "market_type": "championship_winner", "bookmaker_name": "BookA", "implied_probability": 0.1}]

    result = scan_multibook_arbitrage(lines)

    assert result["synthetic_field_arbs"] == []
    assert result["incomplete_hedges"]
    assert result["incomplete_hedges"][0]["diagnostic"] in {"incomplete field", "missing team outcomes"}


def test_stake_sizing_balances_guaranteed_profit():
    lines = [
        {"event_id": "evt", "team": "Knicks", "market_type": "game_winner", "bookmaker_name": "BookA", "implied_probability": 0.45},
        {"event_id": "evt", "team": "Spurs", "market_type": "game_winner", "bookmaker_name": "BookB", "implied_probability": 0.50},
    ]

    arb = scan_multibook_arbitrage(lines, bankroll=1000)["sportsbook_multibook_arbs"][0]

    assert round(arb["stake_a"] + arb["stake_b"], 2) == 1000.0
    assert arb["guaranteed_profit_amount"] > 0


@patch("src.cli.fetch_futures")
@patch("src.cli.fetch_odds")
def test_scan_multibook_arb_cli_smoke(mock_fetch_odds, mock_fetch_futures, capsys):
    from src.cli import scan_multibook_arb

    mock_fetch_odds.return_value = [
        {"event_id": "evt", "team": "Knicks", "market_type": "game_winner", "bookmaker_name": "BookA", "implied_probability": 0.45},
        {"event_id": "evt", "team": "Spurs", "market_type": "game_winner", "bookmaker_name": "BookB", "implied_probability": 0.50},
    ]
    mock_fetch_futures.return_value = {"supported": False, "futures": []}

    result = scan_multibook_arb(sport_key="basketball_nba")

    assert len(result["sportsbook_multibook_arbs"]) == 1
    assert "Sportsbook multibook arbs: 1" in capsys.readouterr().out
