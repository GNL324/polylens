from unittest.mock import patch

from src.analysis.live_arbitrage import scan_live_arbitrage
from src.analysis.live_match_diagnostics import explain_live_matches
from src.analysis.sportsbook_matching import sportsbook_match_diagnostics


PM_LAKERS = {"conditionId": "pm-lakers", "title": "Will the Los Angeles Lakers beat the Boston Celtics?", "outcomePrices": "[0.45,0.55]"}
PM_KNICKS_FINALS = {"conditionId": "pm-knicks", "title": "Will the New York Knicks win the 2026 NBA Finals?", "outcomePrices": "[0.3,0.7]"}


def line(**overrides):
    row = {"event_id": "evt", "league": "NBA", "team": "Los Angeles Lakers", "opponent": "Boston Celtics", "market_type": "game_winner", "implied_probability": 0.6, "bookmaker_name": "Book", "commence_time": "2026-06-10T00:00:00Z"}
    row.update(overrides)
    return row


def first_reason(pm, sportsbook_line):
    return sportsbook_match_diagnostics([pm], [sportsbook_line])[0]["rejection_reason"]


def test_team_mismatch_diagnostic():
    assert first_reason(PM_LAKERS, line(team="Denver Nuggets", opponent="Boston Celtics")) == "team mismatch"


def test_market_type_mismatch_diagnostic():
    assert first_reason(PM_KNICKS_FINALS, line(team="New York Knicks", opponent="Boston Celtics", market_type="game_winner")) == "market type mismatch"


def test_spread_mismatch_diagnostic():
    assert first_reason(PM_LAKERS, line(market_type="spread", line=None)) == "spread mismatch"


def test_total_mismatch_diagnostic():
    assert first_reason(PM_LAKERS, line(market_type="total", line=None)) == "total mismatch"


def test_league_mismatch_diagnostic():
    assert first_reason(PM_LAKERS, line(league="MLB")) == "league mismatch"


def test_live_match_json_summary_and_samples():
    result = explain_live_matches([PM_LAKERS], [], [line(team="Denver Nuggets", opponent="Boston Celtics")], rejected_only=True)
    assert result["matches_attempted"] == 1
    assert result["matches_rejected"] == 1
    assert result["top_rejection_reasons"] == {"team mismatch": 1}
    assert result["rejected_matches_sample"][0]["parsed_fields"]


def test_scan_live_arb_includes_live_match_summary():
    result = scan_live_arbitrage([PM_LAKERS], [], sportsbook_lines=[line(team="Denver Nuggets", opponent="Boston Celtics")])
    assert "live_match_summary" in result
    assert result["live_match_summary"]["top_rejection_reasons"]["team mismatch"] == 1


@patch("src.cli.fetch_odds")
@patch("src.cli.KalshiClient")
@patch("src.cli.PolymarketClient")
def test_explain_live_matches_cli_smoke(mock_poly, mock_kalshi, mock_fetch, capsys):
    from src.cli import explain_live_matches_cli

    mock_poly.return_value.get_active_markets.return_value = [PM_LAKERS]
    mock_kalshi.return_value.get_markets.return_value = []
    mock_fetch.return_value = [line(team="Denver Nuggets", opponent="Boston Celtics")]
    result = explain_live_matches_cli(sport_key="basketball_nba", as_json=True, rejected_only=True)
    assert result["matches_rejected"] == 1
    assert "team mismatch" in capsys.readouterr().out
