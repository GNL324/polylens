from src.analysis.prop_arbitrage import scan_prop_arbitrage
from src.analysis.prop_matching import match_prop_pairs_with_metrics


def prop(player, side, line=28.5, event="evt", market="player_points", prob=0.5):
    return {"sport": "basketball_nba", "league": "NBA", "event_id": event, "player": player, "market_type": market, "line": line, "side": side, "bookmaker": f"Book-{player}-{side}", "implied_probability": prob}


def test_grouped_matching_returns_same_valid_matches_without_player_mismatch_rejects():
    props = [
        prop("Jalen Brunson", "over", prob=0.45),
        prop("Jalen Brunson", "under", prob=0.50),
        prop("Josh Hart", "over", line=9.5),
        prop("Josh Hart", "under", line=9.5),
        prop("Mikal Bridges", "over", line=15.5),
    ]

    result = match_prop_pairs_with_metrics(props)

    assert len(result["matches"]) == 2
    assert {match["player"] for match in result["matches"]} == {"Jalen Brunson", "Josh Hart"}
    assert all(reject["rejection_reason"] != "player mismatch" for reject in result["rejects"])
    assert result["rejects"] == [{"player": "Mikal Bridges", "market_type": "player_points", "line": 15.5, "event_id": "evt", "rejection_reason": "missing opposite side"}]


def test_grouping_complexity_is_substantially_reduced():
    props = []
    for index in range(50):
        props.append(prop(f"Player {index}", "over", line=index + 0.5))
        props.append(prop(f"Player {index}", "under", line=index + 0.5))

    result = match_prop_pairs_with_metrics(props)

    assert result["metrics"]["comparisons_before_grouping"] == 4950
    assert result["metrics"]["comparisons_after_grouping"] == 50
    assert result["metrics"]["grouping_reduction_percent"] > 98
    assert result["rejects"] == []


def test_prop_arbitrage_exposes_grouping_metrics_and_no_mismatch_rejects():
    props = [
        prop("Jalen Brunson", "over", prob=0.45),
        prop("Jalen Brunson", "under", prob=0.50),
        prop("Josh Hart", "over", line=9.5, prob=0.53),
        prop("Josh Hart", "under", line=9.5, prob=0.52),
    ]

    result = scan_prop_arbitrage(props)

    assert result["matched_prop_pairs"] == 2
    assert result["rejection_reasons"] == {"total implied probability >= 1": 1}
    assert result["comparisons_after_grouping"] == 2
    assert result["comparisons_before_grouping"] == 6
    assert result["grouping_reduction_percent"] > 60
    assert "player mismatch" not in result["rejection_reasons"]
