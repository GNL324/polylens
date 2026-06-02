from unittest.mock import patch

from src.analysis.odds_normalization import normalize_futures_events
from src.analysis.sportsbook_matching import match_sportsbook_lines, sportsbook_match_diagnostics


def futures_event(team="New York Knicks", market_key="outrights", price=1200):
    return {
        "id": "nba-finals",
        "sport_key": "basketball_nba",
        "sport_title": "NBA",
        "title": "2026 NBA Championship Winner",
        "bookmakers": [
            {
                "key": "dk",
                "title": "DraftKings",
                "markets": [
                    {
                        "key": market_key,
                        "last_update": "2026-06-01T00:00:00Z",
                        "outcomes": [{"name": team, "price": price}],
                    }
                ],
            }
        ],
    }


def test_normalize_championship_futures():
    rows = normalize_futures_events([futures_event()])

    assert rows[0]["market_type"] == "championship_winner"
    assert rows[0]["league"] == "NBA"
    assert rows[0]["team"] == "New York Knicks"
    assert rows[0]["implied_probability"] == 0.0769


def test_knicks_nba_finals_matches_sportsbook_championship_future():
    pm = [{"conditionId": "pm-knicks", "title": "Will the New York Knicks win the 2026 NBA Finals?", "outcomePrices": "[0.08,0.92]"}]
    lines = normalize_futures_events([futures_event()])

    candidates = match_sportsbook_lines(pm, lines)

    assert len(candidates) == 1
    assert candidates[0]["sportsbook_team"] == "New York Knicks"
    assert candidates[0]["market_type"] == "championship_winner"


def test_celtics_championship_future_matches():
    pm = [{"conditionId": "pm-celtics", "title": "Will the Boston Celtics win the 2026 NBA Finals?", "outcomePrices": "[0.2,0.8]"}]
    lines = normalize_futures_events([futures_event(team="Boston Celtics", price=450)])

    assert len(match_sportsbook_lines(pm, lines)) == 1


def test_championship_vs_game_market_rejected():
    pm = [{"conditionId": "pm-knicks", "title": "Will the New York Knicks win the 2026 NBA Finals?"}]
    lines = [{"event_id": "evt", "league": "NBA", "team": "New York Knicks", "opponent": "San Antonio Spurs", "market_type": "game_winner", "implied_probability": 0.6, "bookmaker_name": "Book"}]

    diagnostics = sportsbook_match_diagnostics(pm, lines)

    assert diagnostics[0]["accepted"] is False
    assert diagnostics[0]["rejection_reason"] == "market type mismatch"


def test_championship_vs_conference_market_rejected():
    pm = [{"conditionId": "pm-knicks", "title": "Will the New York Knicks win the 2026 NBA Finals?"}]
    lines = normalize_futures_events([futures_event(market_key="conference_winner")])

    diagnostics = sportsbook_match_diagnostics(pm, lines)

    assert diagnostics[0]["accepted"] is False
    assert diagnostics[0]["rejection_reason"] == "market type mismatch"


def test_championship_vs_player_prop_rejected():
    pm = [{"conditionId": "pm-knicks", "title": "Will the New York Knicks win the 2026 NBA Finals?"}]
    lines = [{"event_id": "prop", "league": "NBA", "team": "Jalen Brunson", "market_type": "player_prop", "implied_probability": 0.55, "bookmaker_name": "Book"}]

    diagnostics = sportsbook_match_diagnostics(pm, lines)

    assert diagnostics[0]["accepted"] is False
    assert diagnostics[0]["rejection_reason"] == "market type mismatch"


@patch("src.cli.OddsAPIClient")
def test_fetch_futures_cli_smoke(mock_client, capsys):
    from src.cli import fetch_futures

    mock_client.return_value.get_futures.return_value = [futures_event()]
    rows = fetch_futures("basketball_nba")

    assert rows[0]["market_type"] == "championship_winner"
    assert "Fetched sportsbook futures" in capsys.readouterr().out


@patch("src.cli.fetch_futures")
@patch("src.cli.fetch_odds")
@patch("src.cli.KalshiClient")
@patch("src.cli.PolymarketClient")
def test_scan_live_arb_includes_sportsbook_futures(mock_poly, mock_kalshi, mock_fetch_odds, mock_fetch_futures):
    from src.cli import scan_live_arb

    mock_poly.return_value.get_active_markets.return_value = [{"conditionId": "pm-knicks", "title": "Will the New York Knicks win the 2026 NBA Finals?", "outcomePrices": "[0.08,0.92]"}]
    mock_poly.return_value.last_active_market_search_diagnostics = {}
    mock_kalshi.return_value.get_markets.return_value = []
    mock_fetch_odds.return_value = []
    mock_fetch_futures.return_value = [{"event_id": "nba-finals", "league": "NBA", "team": "New York Knicks", "market_type": "championship_winner", "implied_probability": 0.1, "bookmaker_name": "Book", "odds": 900}]

    result = scan_live_arb(sport_key="basketball_nba", keyword="knicks", limit=5, as_json=True)

    assert result["matches_found_by_venue_pair"]["polymarket_sportsbook"] == 1
    assert result["live_match_summary"]["futures_matches_accepted"] == 1
