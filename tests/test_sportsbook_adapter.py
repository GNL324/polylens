from unittest.mock import patch

import pytest

from src.adapters.odds_api import MissingOddsAPIKey, OddsAPIClient
from src.analysis.arb_pricing import enrich_sportsbook_candidates_with_pricing
from src.analysis.odds_normalization import american_to_implied_probability, normalize_odds_events
from src.analysis.sportsbook_matching import match_sportsbook_lines


class FakeResponse:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return b'[{"key":"basketball_nba","title":"NBA","group":"Basketball"}]'


def test_odds_api_parsing_requires_key(monkeypatch):
    monkeypatch.delenv("ODDS_API_KEY", raising=False)
    client = OddsAPIClient(api_key=None)
    with pytest.raises(MissingOddsAPIKey, match="ODDS_API_KEY"):
        client.list_sports()


def test_odds_api_parsing_with_mocked_response(tmp_path):
    client = OddsAPIClient(api_key="test", raw_dir=tmp_path)
    with patch("src.adapters.odds_api.urlopen", return_value=FakeResponse()):
        sports = client.list_sports()
    assert sports[0]["key"] == "basketball_nba"


def test_american_odds_to_implied_probability():
    assert american_to_implied_probability(-110) == 0.5238
    assert american_to_implied_probability(150) == 0.4


def test_sportsbook_normalization():
    events = [{
        "id": "evt1",
        "sport_key": "basketball_nba",
        "home_team": "Los Angeles Lakers",
        "away_team": "Boston Celtics",
        "commence_time": "2026-06-10T00:00:00Z",
        "bookmakers": [{"key": "dk", "title": "DraftKings", "markets": [{"key": "h2h", "last_update": "2026-06-01T00:00:00Z", "outcomes": [{"name": "Los Angeles Lakers", "price": -120}, {"name": "Boston Celtics", "price": 100}]}]}],
    }]
    rows = normalize_odds_events(events)
    assert rows[0]["bookmaker_name"] == "DraftKings"
    assert rows[0]["league"] == "NBA"
    assert rows[0]["market_type"] == "game_winner"
    assert rows[0]["implied_probability"] == 0.5455


def test_sportsbook_matching_by_team_league_date():
    pm = [{"conditionId": "pm-lakers", "title": "Will the Lakers beat the Celtics tonight?", "size": 1, "price": 0.4}]
    lines = [{"event_id": "evt", "league": "NBA", "team": "Los Angeles Lakers", "opponent": "Boston Celtics", "market_type": "game_winner", "implied_probability": 0.55, "bookmaker_name": "Book", "commence_time": "2026-06-10T00:00:00Z"}]
    candidates = match_sportsbook_lines(pm, lines)
    assert len(candidates) == 1
    assert candidates[0]["sportsbook_team"] == "Los Angeles Lakers"


def test_sportsbook_matching_rejects_ambiguous_team():
    pm = [{"conditionId": "pm-lakers", "title": "Will the Lakers beat the Celtics tonight?", "size": 1, "price": 0.4}]
    lines = [{"event_id": "evt", "league": "NBA", "team": "Denver Nuggets", "opponent": "Boston Celtics", "market_type": "game_winner", "implied_probability": 0.55, "bookmaker_name": "Book"}]
    assert match_sportsbook_lines(pm, lines) == []


def test_sportsbook_arbitrage_candidate_output():
    candidates = [{"polymarket_id": "pm-lakers", "polymarket_title": "Will the Lakers beat the Celtics tonight?", "sportsbook_implied_probability": 0.6}]
    trades = [{"conditionId": "pm-lakers", "title": "Will the Lakers beat the Celtics tonight?", "outcome": "Yes", "outcomeIndex": 0, "price": 0.45, "size": 1, "timestamp": 1}]
    priced = enrich_sportsbook_candidates_with_pricing(candidates, trades)
    assert priced[0]["estimated_edge"] == 0.15
    assert priced[0]["theoretical_sportsbook_arbitrage_exists"] is True


@patch("src.cli.OddsAPIClient")
def test_list_sportsbooks_cli_smoke(mock_client, capsys):
    from src.cli import list_sportsbooks

    mock_client.return_value.list_sports.return_value = [{"key": "basketball_nba", "title": "NBA", "group": "Basketball"}]
    list_sportsbooks()
    assert "basketball_nba" in capsys.readouterr().out


@patch("src.cli.OddsAPIClient")
def test_fetch_odds_cli_smoke(mock_client, capsys):
    from src.cli import fetch_odds

    mock_client.return_value.get_odds.return_value = []
    fetch_odds("basketball_nba")
    assert "Fetched sportsbook lines" in capsys.readouterr().out


@patch("src.cli.PolymarketClient")
@patch("src.cli.fetch_odds")
def test_scan_sportsbook_arb_cli_smoke(mock_fetch_odds, mock_poly_client, capsys):
    from src.cli import scan_sportsbook_arb

    mock_poly_client.return_value.get_user_trades.return_value = [{"conditionId": "pm-lakers", "title": "Will the Lakers beat the Celtics tonight?", "outcome": "Yes", "outcomeIndex": 0, "price": 0.45, "size": 1, "timestamp": 1}]
    mock_fetch_odds.return_value = [{"event_id": "evt", "league": "NBA", "team": "Los Angeles Lakers", "opponent": "Boston Celtics", "market_type": "game_winner", "implied_probability": 0.6, "bookmaker_name": "Book"}]
    scan_sportsbook_arb("0x" + "6" * 40, "basketball_nba")
    assert "Sportsbook arbitrage candidates" in capsys.readouterr().out
