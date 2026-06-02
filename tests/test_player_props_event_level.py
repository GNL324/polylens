from unittest.mock import patch

from src.adapters.odds_api import OddsAPIClient, OddsAPIError


def event_payload():
    return [{"id": "evt1", "home_team": "New York Knicks", "away_team": "San Antonio Spurs"}, {"id": "evt2", "home_team": "Boston Celtics", "away_team": "Los Angeles Lakers"}]


def event_odds(event_id="evt1"):
    return {"id": event_id, "sport_key": "basketball_nba", "sport_title": "NBA", "home_team": "New York Knicks", "away_team": "San Antonio Spurs", "bookmakers": [{"key": "dk", "title": "DraftKings", "markets": [{"key": "player_points", "outcomes": [{"name": "Over", "description": "Jalen Brunson", "price": -110, "point": 28.5}, {"name": "Under", "description": "Jalen Brunson", "price": -105, "point": 28.5}]}]}]}


def test_event_discovery():
    client = OddsAPIClient(api_key="test")
    with patch.object(client, "_get_json", return_value=event_payload()) as mock_get:
        events = client.get_events("basketball_nba")
    assert len(events) == 2
    mock_get.assert_called_once_with("sports/basketball_nba/events", {}, raw_key="events_basketball_nba")


def test_event_prop_fetch_uses_event_endpoint():
    client = OddsAPIClient(api_key="test")
    with patch.object(client, "_get_json", return_value=event_odds()) as mock_get:
        result = client.get_event_props("basketball_nba", "evt1", markets="player_points")
    assert result["supported"] is True
    assert result["events"][0]["id"] == "evt1"
    assert mock_get.call_args.args[0] == "sports/basketball_nba/events/evt1/odds"


def test_unsupported_market_handling():
    client = OddsAPIClient(api_key="test")
    with patch.object(client, "_get_json", side_effect=OddsAPIError("GET failed with HTTP 422: Markets not supported by this endpoint: player_points")):
        result = client.get_event_props("basketball_nba", "evt1", markets="player_points")
    assert result["supported"] is False
    assert result["unsupported_markets"] == [{"supported": False, "reason": "market unsupported", "market": "player_points"}]


def test_aggregation_across_events():
    client = OddsAPIClient(api_key="test")
    with patch.object(client, "get_events", return_value=event_payload()), patch.object(client, "get_event_props", side_effect=[{"supported": True, "events": [event_odds("evt1")]}, {"supported": True, "events": [event_odds("evt2")]}]):
        result = client.get_player_props("basketball_nba", markets="player_points")
    assert result["events_discovered"] == 2
    assert result["events_scanned"] == 2
    assert len(result["events"]) == 2
    assert result["prop_markets_supported"] == ["player_points"]


@patch("src.cli.OddsAPIClient")
def test_debug_player_props_cli_smoke(mock_client, capsys):
    from src.cli import debug_player_props
    mock_client.return_value.get_player_props.return_value = {"events": [event_odds("evt1")], "events_discovered": 1, "events_scanned": 1, "events_failed": 0, "prop_markets_supported": ["player_points"], "prop_markets_rejected": []}
    result = debug_player_props("basketball_nba")
    assert result["events"][0]["prop_count"] == 2
    assert "evt1" in capsys.readouterr().out
