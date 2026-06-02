from unittest.mock import patch

from src.analysis.futures_inventory import summarize_futures_inventory
from src.analysis.synthetic_field import build_synthetic_field


def raw_futures_event():
    return {
        "id": "nba-title",
        "sport_key": "basketball_nba_championship_winner",
        "sport_title": "NBA Championship Winner",
        "bookmakers": [
            {"key": "dk", "title": "DraftKings", "markets": [{"key": "outrights", "outcomes": [{"name": "Knicks", "price": 170}, {"name": "Spurs", "price": -185}, {"name": "Celtics", "price": 450}, {"name": "Lakers", "price": 900}]}]},
            {"key": "fd", "title": "FanDuel", "markets": [{"key": "outrights", "outcomes": [{"name": "Knicks", "price": 160}, {"name": "Spurs", "price": -180}, {"name": "Celtics", "price": 430}, {"name": "Lakers", "price": 850}]}]},
        ],
    }


def test_full_futures_inventory_retained():
    from src.analysis.odds_normalization import normalize_futures_events

    raw = [raw_futures_event()]
    normalized = normalize_futures_events(raw)
    summary = summarize_futures_inventory(raw, normalized)

    assert summary["futures_raw_outcomes"] == 8
    assert summary["futures_normalized_outcomes"] == 8
    assert summary["futures_retained_outcomes"] == 8
    assert set(summary["normalized_unique_outcomes"]) == {"Knicks", "Spurs", "Celtics", "Lakers"}
    assert summary["markets"][0]["outcome_count"] == 4


def test_no_outcome_truncation_in_normalizer():
    from src.analysis.odds_normalization import normalize_futures_events

    normalized = normalize_futures_events([raw_futures_event()])

    assert len(normalized) == 8
    assert [row["team"] for row in normalized].count("Knicks") == 2


def test_synthetic_field_receives_all_outcomes_before_deduping():
    from src.analysis.odds_normalization import normalize_futures_events

    normalized = normalize_futures_events([raw_futures_event()])
    field = build_synthetic_field(normalized, "Knicks")

    assert field["futures_retained_outcomes"] == 8
    assert field["unique_outcomes"] == 4
    assert field["duplicate_outcomes_removed"] == 4
    assert set(row["team"] for row in field["field_outcomes"]) == {"Spurs", "Celtics", "Lakers"}


@patch("src.cli.OddsAPIClient")
def test_debug_futures_inventory_cli_smoke(mock_client, capsys):
    from src.cli import debug_futures_inventory

    mock_client.return_value.get_futures.return_value = {"supported": True, "reason": None, "futures_sport_key": "basketball_nba_championship_winner", "events": [raw_futures_event()]}
    result = debug_futures_inventory("basketball_nba")

    assert result["futures_raw_outcomes"] == 8
    assert result["futures_normalized_outcomes"] == 8
    assert "Raw outcomes: 8" in capsys.readouterr().out
