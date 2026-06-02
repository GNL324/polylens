from unittest.mock import patch

from src.analysis.multibook_arbitrage import scan_multibook_arbitrage
from src.analysis.synthetic_field import build_synthetic_field, debug_synthetic_field, dedupe_best_outcomes


def duplicate_futures():
    return [
        {"team": "Knicks", "bookmaker_name": "DraftKings", "odds": 155, "implied_probability": 0.3922, "market_type": "championship_winner", "event_id": "nba", "league": "NBA"},
        {"team": "Knicks", "bookmaker_name": "FanDuel", "odds": 160, "implied_probability": 0.3846, "market_type": "championship_winner", "event_id": "nba", "league": "NBA"},
        {"team": "Knicks", "bookmaker_name": "BetRivers", "odds": 160, "implied_probability": 0.3846, "market_type": "championship_winner", "event_id": "nba", "league": "NBA"},
        {"team": "Knicks", "bookmaker_name": "BetMGM", "odds": 170, "implied_probability": 0.3704, "market_type": "championship_winner", "event_id": "nba", "league": "NBA"},
        {"team": "Spurs", "bookmaker_name": "DraftKings", "odds": -180, "implied_probability": 0.6429, "market_type": "championship_winner", "event_id": "nba", "league": "NBA"},
        {"team": "Spurs", "bookmaker_name": "FanDuel", "odds": -185, "implied_probability": 0.6491, "market_type": "championship_winner", "event_id": "nba", "league": "NBA"},
    ]


def test_duplicate_team_across_books_keeps_one_best_price():
    best, diagnostics = dedupe_best_outcomes(duplicate_futures())

    assert len(best) == 2
    assert diagnostics["unique_outcomes"] == 2
    assert diagnostics["duplicate_outcomes_removed"] == 4
    assert diagnostics["best_price_source"]["Knicks"]["bookmaker"] == "BetMGM"
    assert diagnostics["best_price_source"]["Knicks"]["implied_probability"] == 0.3704


def test_synthetic_field_uses_unique_other_teams_only():
    field = build_synthetic_field(duplicate_futures(), "Knicks")

    assert [row["team"] for row in field["field_outcomes"]] == ["Spurs"]
    assert field["field_price"] == 0.6429
    assert field["duplicate_outcomes_removed"] == 4


def test_probability_sanity_check_uses_unique_outcomes():
    result = debug_synthetic_field(duplicate_futures(), "Knicks")

    assert result["implied_probability_sum"] == 1.0133
    assert result["implied_probability_sum"] < 1.1


def test_multibook_futures_coverage_uses_deduped_outcomes():
    result = scan_multibook_arbitrage(duplicate_futures())

    assert result["synthetic_field_arbs"] == []
    reject = result["incomplete_hedges"][0]
    assert reject["unique_outcomes"] == 2
    assert reject["duplicate_outcomes_removed"] == 4
    assert reject.get("diagnostic") == "total implied probability >= 1"


@patch("src.cli.fetch_futures")
def test_debug_synthetic_field_cli_smoke(mock_fetch_futures, capsys):
    from src.cli import debug_synthetic_field_cli

    mock_fetch_futures.return_value = {"supported": True, "futures": duplicate_futures()}
    result = debug_synthetic_field_cli(sport_key="basketball_nba", selected_team="Knicks")

    assert result["implied_probability_sum"] == 1.0133
    assert "Selected team: Knicks" in capsys.readouterr().out
