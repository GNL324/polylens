from __future__ import annotations

import sqlite3

import pytest

from src import cli
from src.storage.opportunities import (
    create_scanner_profile,
    get_active_scanner_profile,
    get_scanner_profile_by_name,
    list_scanner_profiles,
    set_active_scanner_profile,
    update_scanner_profile,
)
from src.web.dashboard import (
    deserialize_market_selection,
    deserialize_sport_selection,
    deserialize_sportsbook_selection,
    market_catalog,
    market_display,
    markets_after_sport_change,
    scanner_profile_rows,
    serialize_market_checkboxes,
    serialize_sportsbook_checkboxes,
    sport_display,
    sportsbook_display,
    validate_scanner_profile_payload,
)


def test_default_profile_creation(tmp_path) -> None:
    db_path = tmp_path / "opportunities.db"

    active = get_active_scanner_profile(db_path=str(db_path))

    assert active is not None
    assert active["name"] == "NBA Player Points"
    assert active["sport"] == "basketball_nba"
    assert active["markets"] == ["player_points"]
    assert active["sportsbooks"] == []
    assert active["bankroll"] == 1000
    assert active["interval_seconds"] == 60
    assert active["min_roi"] == 0
    assert active["is_active"] is True


def test_profile_crud_and_active_selection(tmp_path) -> None:
    db_path = tmp_path / "opportunities.db"
    profile_id = create_scanner_profile(
        db_path=str(db_path),
        name="NBA Assists",
        sport="basketball_nba",
        markets=["player_assists"],
        sportsbooks=["fanduel", "bovada"],
        bankroll=500,
        interval_seconds=45,
        min_roi=0.02,
    )

    updated = update_scanner_profile(profile_id, db_path=str(db_path), bankroll=750, sportsbooks="draftkings,bovada")
    assert updated is not None
    assert updated["bankroll"] == 750
    assert updated["sportsbooks"] == ["draftkings", "bovada"]

    active = set_active_scanner_profile(profile_id, db_path=str(db_path))
    assert active is not None
    assert active["name"] == "NBA Assists"
    profiles = list_scanner_profiles(db_path=str(db_path))
    assert [row for row in profiles if row["is_active"]][0]["id"] == profile_id


def test_dashboard_scanner_profile_rows(tmp_path) -> None:
    db_path = tmp_path / "opportunities.db"
    create_scanner_profile(
        db_path=str(db_path),
        name="NBA Rebounds",
        sport="basketball_nba",
        markets="player_rebounds",
        sportsbooks="fanduel,bovada",
    )

    rows = scanner_profile_rows(str(db_path))
    names = {row["name"]: row for row in rows}
    assert names["NBA Player Points"]["sport"] == "NBA"
    assert names["NBA Player Points"]["markets"] == "Player Points"
    assert names["NBA Player Points"]["sportsbooks"] == "All sportsbooks"
    assert names["NBA Rebounds"]["sport"] == "NBA"
    assert names["NBA Rebounds"]["markets"] == "Player Rebounds"
    assert names["NBA Rebounds"]["sportsbooks"] == "FanDuel, Bovada"


def test_sport_catalog_friendly_names() -> None:
    assert deserialize_sport_selection("basketball_nba") == "basketball_nba"
    assert sport_display("basketball_nba") == "NBA"
    assert sport_display("americanfootball_nfl") == "NFL"
    assert deserialize_sport_selection("unknown") == ""


def test_market_catalog_friendly_names() -> None:
    assert market_display(["player_points", "player_rebounds"]) == "Player Points, Player Rebounds"
    assert deserialize_market_selection("player_assists, unknown, player_blocks") == ["player_assists", "player_blocks"]


def test_nba_and_wnba_share_basketball_markets() -> None:
    nba = [row["key"] for row in market_catalog("basketball_nba")]
    wnba = [row["key"] for row in market_catalog("basketball_wnba")]

    assert nba == wnba
    assert nba == [
        "player_points",
        "player_rebounds",
        "player_assists",
        "player_threes",
        "player_blocks",
        "player_steals",
        "player_turnovers",
    ]


def test_mlb_shows_mlb_markets_only() -> None:
    keys = [row["key"] for row in market_catalog("baseball_mlb")]

    assert keys == [
        "batter_home_runs",
        "batter_hits",
        "batter_rbis",
        "batter_runs_scored",
        "batter_total_bases",
        "pitcher_strikeouts",
        "pitcher_outs",
    ]
    assert "player_points" not in keys


def test_nfl_shows_nfl_markets_only() -> None:
    keys = [row["key"] for row in market_catalog("americanfootball_nfl")]

    assert keys == [
        "player_pass_tds",
        "player_pass_yds",
        "player_rush_yds",
        "player_receptions",
        "player_reception_yds",
        "player_anytime_td",
        "player_kicking_points",
    ]
    assert "player_rebounds" not in keys


def test_nhl_shows_nhl_markets_only() -> None:
    keys = [row["key"] for row in market_catalog("icehockey_nhl")]

    assert keys == [
        "player_points",
        "player_goals",
        "player_assists",
        "player_shots_on_goal",
        "player_power_play_points",
        "goalie_saves",
    ]
    assert "player_rebounds" not in keys


def test_market_checkbox_serialization_uses_catalog_order() -> None:
    selected = serialize_market_checkboxes(
        {
            "player_rebounds": True,
            "player_points": True,
            "player_assists": False,
            "unknown": True,
        }
    )

    assert selected == ["player_points", "player_rebounds"]


def test_market_checkbox_serialization_is_sport_specific() -> None:
    selected = serialize_market_checkboxes(
        {"batter_rbis": True, "batter_home_runs": True, "player_points": True},
        sport="baseball_mlb",
    )

    assert selected == ["batter_home_runs", "batter_rbis"]


def test_market_checkbox_deserialization_filters_unknowns() -> None:
    selected = deserialize_market_selection("player_rebounds, unknown, player_points, player_rebounds")

    assert selected == ["player_rebounds", "player_points"]


def test_changing_sport_clears_invalid_markets() -> None:
    assert markets_after_sport_change(["player_points", "player_rebounds", "batter_rbis"], "baseball_mlb") == ["batter_rbis"]
    assert markets_after_sport_change(["batter_rbis", "player_points"], "icehockey_nhl") == ["player_points"]


def test_profile_edit_preselection_helpers(tmp_path) -> None:
    db_path = tmp_path / "opportunities.db"
    profile_id = create_scanner_profile(
        db_path=str(db_path),
        name="NBA Combo",
        sport="basketball_nba",
        markets=["player_points", "player_rebounds"],
        sportsbooks=["fanduel", "bovada"],
    )

    profile = next(row for row in list_scanner_profiles(db_path=str(db_path)) if row["id"] == profile_id)

    assert deserialize_sport_selection(profile["sport"]) == "basketball_nba"
    assert deserialize_market_selection(profile["markets"]) == ["player_points", "player_rebounds"]
    assert deserialize_sportsbook_selection(profile["sportsbooks"]) == ["fanduel", "bovada"]


def test_scanner_profile_validation_blocks_no_sport() -> None:
    errors = validate_scanner_profile_payload({"name": "Bad", "sport": "", "markets": ["player_points"]})

    assert "Select one sport" in errors


def test_scanner_profile_validation_blocks_no_markets() -> None:
    errors = validate_scanner_profile_payload({"name": "Bad", "sport": "basketball_nba", "markets": []})

    assert "Select at least one market" in errors


def test_scanner_profile_validation_rejects_invalid_market_for_sport() -> None:
    errors = validate_scanner_profile_payload({"name": "Bad", "sport": "baseball_mlb", "markets": ["player_rebounds"]})

    assert "Select at least one market" in errors
    assert "Selected markets are not valid for MLB: player_rebounds" in errors


def test_active_profile_display_uses_sport_specific_market_names(tmp_path) -> None:
    db_path = tmp_path / "opportunities.db"
    create_scanner_profile(
        db_path=str(db_path),
        name="MLB Power",
        sport="baseball_mlb",
        markets=["batter_home_runs", "batter_rbis"],
        is_active=True,
    )

    rows = scanner_profile_rows(str(db_path))
    profile = {row["name"]: row for row in rows}["MLB Power"]

    assert profile["sport"] == "MLB"
    assert profile["markets"] == "Batter Home Runs, Batter RBIs"


def test_sportsbook_checkbox_serialization_uses_catalog_order() -> None:
    selected = serialize_sportsbook_checkboxes(
        {
            "bovada": True,
            "fanduel": True,
            "draftkings": False,
            "unknown": True,
        }
    )

    assert selected == ["fanduel", "bovada"]


def test_sportsbook_checkbox_deserialization_filters_unknowns() -> None:
    selected = deserialize_sportsbook_selection("FanDuel, bovada, unknown, fanduel")

    assert selected == ["fanduel", "bovada"]


def test_sportsbook_empty_selection_means_all_books() -> None:
    assert serialize_sportsbook_checkboxes({"fanduel": False, "bovada": False}) == []
    assert deserialize_sportsbook_selection("") == []
    assert sportsbook_display([]) == "All sportsbooks"


def test_profile_edit_stores_checkbox_selection(tmp_path) -> None:
    db_path = tmp_path / "opportunities.db"
    profile_id = create_scanner_profile(
        db_path=str(db_path),
        name="NBA Points Filtered",
        sport="basketball_nba",
        markets=["player_points"],
        sportsbooks=serialize_sportsbook_checkboxes({"fanduel": True, "bovada": True}),
    )

    updated = update_scanner_profile(
        profile_id,
        db_path=str(db_path),
        sportsbooks=serialize_sportsbook_checkboxes({"betrivers": True, "bovada": True}),
    )

    assert updated is not None
    assert updated["sportsbooks"] == ["betrivers", "bovada"]
    assert sportsbook_display(updated["sportsbooks"]) == "BetRivers, Bovada"


def test_cli_profile_and_sportsbook_filtering(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / "opportunities.db"
    create_scanner_profile(
        db_path=str(db_path),
        name="Filtered",
        sport="basketball_nba",
        markets=["player_points"],
        sportsbooks=["fanduel", "bovada"],
        bankroll=1000,
        interval_seconds=60,
        min_roi=0.03,
        is_active=True,
    )
    captured = {}

    def fake_fetch(sport_key, event_id=None, bookmaker=None, region="us", markets=None, as_json=False, quiet=False):
        captured["fetch"] = {"sport_key": sport_key, "bookmaker": bookmaker, "markets": markets}
        return [
            {"bookmaker_key": "fanduel", "player": "A"},
            {"bookmaker_key": "bovada", "player": "A"},
            {"bookmaker_key": "draftkings", "player": "A"},
        ]

    def fake_scan(props, **kwargs):
        captured["props"] = props
        captured["scan_kwargs"] = kwargs
        return {
            "props_fetched": len(props),
            "matched_prop_pairs": 1,
            "rejected_pairs": 0,
            "prop_arbitrage_candidates": [],
            "scan_duration_seconds": kwargs.get("scan_duration_seconds"),
            "rejection_reasons": {},
        }

    monkeypatch.setattr(cli, "fetch_player_props", fake_fetch)
    monkeypatch.setattr(cli, "scan_prop_arbitrage", fake_scan)

    result = cli.scan_prop_arb(None, profile="Filtered", as_json=False, db_path=str(db_path))

    assert captured["fetch"] == {"sport_key": "basketball_nba", "bookmaker": "fanduel,bovada", "markets": "player_points"}
    assert [row["bookmaker_key"] for row in captured["props"]] == ["fanduel", "bovada"]
    assert captured["scan_kwargs"]["bankroll"] == 1000
    assert captured["scan_kwargs"]["min_guaranteed_roi"] == 0.03
    assert result["sportsbooks_filter"] == ["fanduel", "bovada"]


def test_cli_overrides_profile_values(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / "opportunities.db"
    create_scanner_profile(
        db_path=str(db_path),
        name="Filtered",
        sport="basketball_nba",
        markets=["player_points"],
        sportsbooks=["fanduel", "bovada"],
        bankroll=1000,
        interval_seconds=60,
        min_roi=0.03,
        is_active=True,
    )
    captured = {}

    def fake_fetch(sport_key, **kwargs):
        captured["fetch"] = {"sport_key": sport_key, **kwargs}
        return []

    monkeypatch.setattr(cli, "fetch_player_props", fake_fetch)

    def fake_scan(props, **kwargs):
        captured["scan_kwargs"] = kwargs
        return {"props_fetched": 0, "matched_prop_pairs": 0, "rejected_pairs": 0, "prop_arbitrage_candidates": [], "rejection_reasons": {}}

    monkeypatch.setattr(cli, "scan_prop_arbitrage", fake_scan)

    cli.scan_prop_arb(
        "basketball_nfl",
        profile="Filtered",
        markets="player_rebounds",
        sportsbooks="draftkings",
        bankroll=250,
        min_guaranteed_roi=0.08,
        as_json=False,
        db_path=str(db_path),
    )

    assert captured["fetch"]["sport_key"] == "basketball_nfl"
    assert captured["fetch"]["bookmaker"] == "draftkings"
    assert captured["fetch"]["markets"] == "player_rebounds"
    assert captured["scan_kwargs"]["bankroll"] == 250
    assert captured["scan_kwargs"]["min_guaranteed_roi"] == 0.08


def test_missing_active_profile_can_be_detected(tmp_path) -> None:
    db_path = tmp_path / "opportunities.db"
    get_active_scanner_profile(db_path=str(db_path))
    with sqlite3.connect(db_path) as conn:
        conn.execute("UPDATE scanner_profiles SET is_active = 0")

    assert get_active_scanner_profile(db_path=str(db_path)) is None
    with pytest.raises(ValueError, match="scanner profile not found"):
        cli.scan_prop_arb(None, profile="__ACTIVE__", db_path=str(db_path))
