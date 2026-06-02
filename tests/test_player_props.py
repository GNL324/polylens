from unittest.mock import patch

from src.analysis.prop_arbitrage import scan_prop_arbitrage
from src.analysis.prop_matching import match_prop_pairs
from src.analysis.prop_normalization import normalize_player_props


def prop_event(market_key="player_points", player="Jalen Brunson", point=28.5):
    return [{
        "id": "evt",
        "sport_key": "basketball_nba",
        "sport_title": "NBA",
        "home_team": "New York Knicks",
        "away_team": "San Antonio Spurs",
        "commence_time": "2026-06-10T00:00:00Z",
        "bookmakers": [{"key": "dk", "title": "DraftKings", "markets": [{"key": market_key, "last_update": "2026-06-01T00:00:00Z", "outcomes": [
            {"name": "Over", "description": player, "price": -110, "point": point},
            {"name": "Under", "description": player, "price": -105, "point": point},
        ]}]}],
    }]


def test_normalize_over_under_player_points():
    rows = normalize_player_props(prop_event())
    assert rows[0]["sport"] == "basketball_nba"
    assert rows[0]["league"] == "NBA"
    assert rows[0]["player"] == "Jalen Brunson"
    assert rows[0]["market_type"] == "player_points"
    assert rows[0]["line"] == 28.5
    assert rows[0]["side"] == "over"
    assert rows[0]["implied_probability"] == 0.5238


def test_normalize_rebounds():
    rows = normalize_player_props(prop_event(market_key="player_rebounds", player="Josh Hart", point=9.5))
    assert rows[0]["market_type"] == "player_rebounds"
    assert rows[0]["player"] == "Josh Hart"


def test_normalize_assists():
    rows = normalize_player_props(prop_event(market_key="player_assists", player="Jalen Brunson", point=7.5))
    assert rows[0]["market_type"] == "player_assists"


def test_reject_different_lines():
    left = {"sport": "basketball_nba", "league": "NBA", "event_id": "evt", "player": "Jalen Brunson", "market_type": "player_points", "line": 28.5, "side": "over"}
    right = {**left, "line": 29.5, "side": "under"}
    matches, rejects = match_prop_pairs([left, right])
    assert matches == []
    assert {reject["rejection_reason"] for reject in rejects} == {"missing opposite side"}


def test_reject_different_players():
    left = {"sport": "basketball_nba", "league": "NBA", "event_id": "evt", "player": "Jalen Brunson", "market_type": "player_points", "line": 28.5, "side": "over"}
    right = {**left, "player": "Josh Hart", "side": "under"}
    matches, rejects = match_prop_pairs([left, right])
    assert matches == []
    assert {reject["rejection_reason"] for reject in rejects} == {"missing opposite side"}


def test_detect_over_under_arb():
    props = [
        {"sport": "basketball_nba", "league": "NBA", "event_id": "evt", "player": "Jalen Brunson", "market_type": "player_points", "line": 28.5, "side": "over", "bookmaker": "BookA", "odds": 130, "implied_probability": 0.4348},
        {"sport": "basketball_nba", "league": "NBA", "event_id": "evt", "player": "Jalen Brunson", "market_type": "player_points", "line": 28.5, "side": "under", "bookmaker": "BookB", "odds": 130, "implied_probability": 0.4348},
    ]
    result = scan_prop_arbitrage(props)
    assert len(result["prop_arbitrage_candidates"]) == 1
    assert result["prop_arbitrage_candidates"][0]["guaranteed_roi"] > 0


def test_reject_no_arb_over_under_pair():
    props = [
        {"sport": "basketball_nba", "league": "NBA", "event_id": "evt", "player": "Jalen Brunson", "market_type": "player_points", "line": 28.5, "side": "over", "bookmaker": "BookA", "implied_probability": 0.53},
        {"sport": "basketball_nba", "league": "NBA", "event_id": "evt", "player": "Jalen Brunson", "market_type": "player_points", "line": 28.5, "side": "under", "bookmaker": "BookB", "implied_probability": 0.52},
    ]
    result = scan_prop_arbitrage(props)
    assert result["prop_arbitrage_candidates"] == []
    assert result["rejection_reasons"] == {"total implied probability >= 1": 1}


def test_prop_stake_sizing():
    props = [
        {"sport": "basketball_nba", "league": "NBA", "event_id": "evt", "player": "Jalen Brunson", "market_type": "player_points", "line": 28.5, "side": "over", "bookmaker": "BookA", "implied_probability": 0.45},
        {"sport": "basketball_nba", "league": "NBA", "event_id": "evt", "player": "Jalen Brunson", "market_type": "player_points", "line": 28.5, "side": "under", "bookmaker": "BookB", "implied_probability": 0.50},
    ]
    arb = scan_prop_arbitrage(props, bankroll=1000)["prop_arbitrage_candidates"][0]
    assert round(arb["stake_over"] + arb["stake_under"], 2) == 1000.0
    assert arb["guaranteed_profit_amount"] > 0


@patch("src.cli.fetch_player_props")
def test_scan_prop_arb_cli_smoke(mock_fetch, capsys):
    from src.cli import scan_prop_arb
    mock_fetch.return_value = [
        {"sport": "basketball_nba", "league": "NBA", "event_id": "evt", "player": "Jalen Brunson", "market_type": "player_points", "line": 28.5, "side": "over", "bookmaker": "BookA", "implied_probability": 0.45},
        {"sport": "basketball_nba", "league": "NBA", "event_id": "evt", "player": "Jalen Brunson", "market_type": "player_points", "line": 28.5, "side": "under", "bookmaker": "BookB", "implied_probability": 0.50},
    ]
    result = scan_prop_arb("basketball_nba")
    assert len(result["prop_arbitrage_candidates"]) == 1
    assert "True arb candidates: 1" in capsys.readouterr().out
