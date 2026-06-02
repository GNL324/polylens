from src.analysis.prop_arbitrage import scan_prop_arbitrage
from src.analysis.prop_normalization import normalize_player_props


def large_prop_event():
    event = {
        "id": "evt",
        "sport_key": "basketball_nba",
        "sport_title": "NBA",
        "home_team": "New York Knicks",
        "away_team": "San Antonio Spurs",
        "large_blob": "x" * 10000,
        "bookmakers": [{
            "key": "dk",
            "title": "DraftKings",
            "large_blob": "y" * 10000,
            "markets": [{
                "key": "player_points",
                "large_blob": "z" * 10000,
                "outcomes": [
                    {"name": "Over", "description": "Jalen Brunson", "price": -110, "point": 28.5},
                    {"name": "Under", "description": "Jalen Brunson", "price": -105, "point": 28.5},
                ],
            }],
        }],
    }
    return event


def test_player_prop_normalization_does_not_retain_nested_raw_payloads():
    rows = normalize_player_props([large_prop_event()])

    assert len(rows) == 2
    for row in rows:
        assert "raw" not in row
        assert "event" not in row
        assert "bookmaker" in row
        assert "market" not in row
        assert row["raw_outcome_name"] in {"Over", "Under"}
        assert row["raw_outcome_description"] == "Jalen Brunson"
        assert row["raw_price"] in {-110, -105}
        assert "large_blob" not in str(row)


def test_prop_arbitrage_diagnostics_are_compact_for_no_arb_pairs():
    props = [
        {"sport": "basketball_nba", "league": "NBA", "event_id": "evt", "player": "Jalen Brunson", "market_type": "player_points", "line": 28.5, "side": "over", "bookmaker": "BookA", "implied_probability": 0.53, "raw": {"huge": "x" * 10000}},
        {"sport": "basketball_nba", "league": "NBA", "event_id": "evt", "player": "Jalen Brunson", "market_type": "player_points", "line": 28.5, "side": "under", "bookmaker": "BookB", "implied_probability": 0.52, "raw": {"huge": "y" * 10000}},
    ]

    result = scan_prop_arbitrage(props)

    assert result["prop_arbitrage_candidates"] == []
    assert result["diagnostics"] == [{"player": "Jalen Brunson", "market_type": "player_points", "line": 28.5, "rejection_reason": "total implied probability >= 1", "implied_probability_sum": 1.05}]
    assert "raw" not in str(result["diagnostics"])
    assert "huge" not in str(result["diagnostics"])


def test_prop_matching_reject_diagnostics_are_compact():
    props = [
        {"sport": "basketball_nba", "league": "NBA", "event_id": "evt", "player": "Jalen Brunson", "market_type": "player_points", "line": 28.5, "side": "over", "raw": {"huge": "x" * 10000}},
        {"sport": "basketball_nba", "league": "NBA", "event_id": "evt", "player": "Josh Hart", "market_type": "player_points", "line": 28.5, "side": "under", "raw": {"huge": "y" * 10000}},
    ]

    result = scan_prop_arbitrage(props)

    assert result["diagnostics"][0]["rejection_reason"] == "player mismatch"
    assert "left" not in result["diagnostics"][0]
    assert "right" not in result["diagnostics"][0]
    assert "raw" not in str(result["diagnostics"])
