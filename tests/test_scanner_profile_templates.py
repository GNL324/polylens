from __future__ import annotations

from src import cli
from src.storage.opportunities import (
    create_scanner_profile_from_template,
    duplicate_scanner_profile,
    get_active_scanner_profile,
    get_scanner_profile,
    get_scanner_profile_by_name,
    resolve_scanner_profile,
    unique_scanner_profile_name,
)
from src.web.dashboard import validate_scanner_profile_payload
from src.web.profile_templates import get_scanner_profile_template, scanner_profile_templates


def test_template_catalog_contains_expected_templates() -> None:
    templates = {template["name"]: template for template in scanner_profile_templates()}

    assert templates["NBA Points - All Books"]["sport"] == "basketball_nba"
    assert templates["NBA Points - All Books"]["markets"] == ["player_points"]
    assert templates["NBA Points - FanDuel/Bovada"]["sportsbooks"] == ["fanduel", "bovada"]
    assert templates["MLB Pitcher Strikeouts"]["markets"] == ["pitcher_strikeouts"]
    assert templates["NFL Anytime TD"]["markets"] == ["player_anytime_td"]
    assert templates["NHL Shots on Goal"]["markets"] == ["player_shots_on_goal"]


def test_recommended_templates_are_marked() -> None:
    recommended = {template["name"] for template in scanner_profile_templates() if template["recommended"]}

    assert recommended == {
        "NBA Points - All Books",
        "MLB Pitcher Strikeouts",
        "NFL Anytime TD",
        "NHL Shots on Goal",
    }


def test_create_profile_from_template(tmp_path) -> None:
    db_path = tmp_path / "opportunities.db"
    template = get_scanner_profile_template("nba_points_all")
    assert template is not None

    profile_id = create_scanner_profile_from_template(template, db_path=str(db_path))
    profile = get_scanner_profile(profile_id, db_path=str(db_path))

    assert profile is not None
    assert profile["name"] == "NBA Points - All Books"
    assert profile["sport"] == "basketball_nba"
    assert profile["markets"] == ["player_points"]
    assert profile["sportsbooks"] == []
    assert profile["template_id"] == "nba_points_all"
    assert profile["is_active"] is False


def test_create_and_activate_from_template(tmp_path) -> None:
    db_path = tmp_path / "opportunities.db"
    template = get_scanner_profile_template("mlb_pitcher_strikeouts")
    assert template is not None

    profile_id = create_scanner_profile_from_template(template, db_path=str(db_path), activate=True)
    active = get_active_scanner_profile(db_path=str(db_path))

    assert active is not None
    assert active["id"] == profile_id
    assert active["name"] == "MLB Pitcher Strikeouts"
    assert active["sport"] == "baseball_mlb"


def test_unique_copy_naming_for_template_creation(tmp_path) -> None:
    db_path = tmp_path / "opportunities.db"
    template = get_scanner_profile_template("nba_points_all")
    assert template is not None

    first = create_scanner_profile_from_template(template, db_path=str(db_path))
    second = create_scanner_profile_from_template(template, db_path=str(db_path))
    third = create_scanner_profile_from_template(template, db_path=str(db_path))

    assert get_scanner_profile(first, db_path=str(db_path))["name"] == "NBA Points - All Books"
    assert get_scanner_profile(second, db_path=str(db_path))["name"] == "NBA Points - All Books Copy"
    assert get_scanner_profile(third, db_path=str(db_path))["name"] == "NBA Points - All Books Copy 2"
    assert unique_scanner_profile_name("NBA Points - All Books", db_path=str(db_path)) == "NBA Points - All Books Copy 3"


def test_duplicate_profile_action(tmp_path) -> None:
    db_path = tmp_path / "opportunities.db"
    template = get_scanner_profile_template("nba_points_fanduel_bovada")
    assert template is not None
    original_id = create_scanner_profile_from_template(template, db_path=str(db_path), activate=True)

    copy_id = duplicate_scanner_profile(original_id, db_path=str(db_path))
    original = get_scanner_profile(original_id, db_path=str(db_path))
    copied = get_scanner_profile(copy_id, db_path=str(db_path))

    assert copied is not None
    assert copied["name"] == "NBA Points - FanDuel/Bovada Copy"
    assert copied["sport"] == original["sport"]
    assert copied["markets"] == original["markets"]
    assert copied["sportsbooks"] == original["sportsbooks"]
    assert copied["bankroll"] == original["bankroll"]
    assert copied["interval_seconds"] == original["interval_seconds"]
    assert copied["min_roi"] == original["min_roi"]
    assert copied["is_active"] is False


def test_template_created_profiles_pass_validation() -> None:
    for template in scanner_profile_templates():
        assert validate_scanner_profile_payload(template) == []


def test_existing_cli_profile_loading_still_works(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "opportunities.db"
    template = get_scanner_profile_template("nba_points_all")
    assert template is not None
    create_scanner_profile_from_template(template, db_path=str(db_path), activate=True)
    captured = {}

    def fake_fetch(sport_key, event_id=None, bookmaker=None, region="us", markets=None, as_json=False, quiet=False):
        captured["fetch"] = {"sport_key": sport_key, "bookmaker": bookmaker, "markets": markets}
        return []

    def fake_scan(props, **kwargs):
        return {"props_fetched": 0, "matched_prop_pairs": 0, "rejected_pairs": 0, "prop_arbitrage_candidates": [], "rejection_reasons": {}}

    monkeypatch.setattr(cli, "fetch_player_props", fake_fetch)
    monkeypatch.setattr(cli, "scan_prop_arbitrage", fake_scan)

    profile = resolve_scanner_profile("NBA Points - All Books", db_path=str(db_path))
    result = cli.scan_prop_arb(None, profile=profile["name"], as_json=False, db_path=str(db_path))

    assert captured["fetch"] == {"sport_key": "basketball_nba", "bookmaker": None, "markets": "player_points"}
    assert result["scanner_profile"] == "NBA Points - All Books"
    assert get_scanner_profile_by_name("NBA Points - All Books", db_path=str(db_path)) is not None
