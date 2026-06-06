from __future__ import annotations

import sqlite3

from src.storage.opportunities import (
    archive_opportunity,
    create_scanner_profile,
    get_active_scanner_profile,
    get_scanner_profile_by_name,
    save_prop_arbitrage_opportunity,
)
from src.web.controls import ALLOWED_COMMANDS
from src.web.dashboard import (
    live_opportunities_empty_message,
    profile_mismatch_note,
    prop_arbitrage_rows,
)


def _save(db_path, **overrides) -> int:
    payload = {
        "player": "Player A",
        "sport": "basketball_nba",
        "prop_type": "player_points",
        "line": 20.5,
        "over_book": "fanduel",
        "over_odds": 110,
        "under_book": "bovada",
        "under_odds": -105,
        "guaranteed_roi": 0.04,
        "guaranteed_profit_amount": 12.5,
        "ranking_score": 90,
        "stake_over": 50,
        "stake_under": 50,
    }
    payload.update(overrides)
    saved_id = save_prop_arbitrage_opportunity(payload, db_path=str(db_path), sport=payload["sport"])
    assert saved_id is not None
    return saved_id


def test_active_profile_filtering_by_sport(tmp_path) -> None:
    db_path = tmp_path / "opportunities.db"
    create_scanner_profile(
        db_path=str(db_path),
        name="NBA",
        sport="basketball_nba",
        markets=["player_points"],
        is_active=True,
    )
    _save(db_path, player="NBA Player", sport="basketball_nba")
    _save(db_path, player="MLB Player", sport="baseball_mlb", prop_type="batter_home_runs")

    rows = prop_arbitrage_rows(db_path, active_profile=get_active_scanner_profile(db_path=str(db_path)))

    assert [row["player"] for row in rows] == ["NBA Player"]


def test_active_profile_filtering_by_markets(tmp_path) -> None:
    db_path = tmp_path / "opportunities.db"
    create_scanner_profile(
        db_path=str(db_path),
        name="NBA Points",
        sport="basketball_nba",
        markets=["player_points"],
        is_active=True,
    )
    _save(db_path, player="Points", prop_type="player_points")
    _save(db_path, player="Rebounds", prop_type="player_rebounds")

    rows = prop_arbitrage_rows(db_path, active_profile=get_active_scanner_profile(db_path=str(db_path)))

    assert [row["player"] for row in rows] == ["Points"]


def test_active_profile_filtering_by_sportsbooks(tmp_path) -> None:
    db_path = tmp_path / "opportunities.db"
    create_scanner_profile(
        db_path=str(db_path),
        name="Filtered Books",
        sport="basketball_nba",
        markets=["player_points"],
        sportsbooks=["fanduel", "bovada"],
        is_active=True,
    )
    _save(db_path, player="Included", over_book="FanDuel", under_book="Bovada")
    _save(db_path, player="Excluded", over_book="fanduel", under_book="draftkings")

    rows = prop_arbitrage_rows(db_path, active_profile=get_active_scanner_profile(db_path=str(db_path)))

    assert [row["player"] for row in rows] == ["Included"]


def test_empty_sportsbook_profile_allows_all_books(tmp_path) -> None:
    db_path = tmp_path / "opportunities.db"
    create_scanner_profile(
        db_path=str(db_path),
        name="All Books",
        sport="basketball_nba",
        markets=["player_points"],
        sportsbooks=[],
        is_active=True,
    )
    _save(db_path, player="Any Book", over_book="fanduel", under_book="draftkings")

    rows = prop_arbitrage_rows(db_path, active_profile=get_active_scanner_profile(db_path=str(db_path)))

    assert [row["player"] for row in rows] == ["Any Book"]


def test_archived_opportunities_hidden_by_default(tmp_path) -> None:
    db_path = tmp_path / "opportunities.db"
    create_scanner_profile(
        db_path=str(db_path),
        name="NBA",
        sport="basketball_nba",
        markets=["player_points"],
        is_active=True,
    )
    _save(db_path, player="Open")
    archived_id = _save(db_path, player="Archived", line=21.5)
    archive_opportunity(archived_id, db_path=str(db_path))

    rows = prop_arbitrage_rows(db_path, active_profile=get_active_scanner_profile(db_path=str(db_path)))

    assert [row["player"] for row in rows] == ["Open"]


def test_manual_filters_apply_after_profile_filter(tmp_path) -> None:
    db_path = tmp_path / "opportunities.db"
    create_scanner_profile(
        db_path=str(db_path),
        name="NBA",
        sport="basketball_nba",
        markets=["player_points"],
        sportsbooks=["fanduel", "bovada"],
        is_active=True,
    )
    _save(db_path, player="Mitchell Robinson")
    _save(db_path, player="Jalen Brunson", line=22.5)

    rows = prop_arbitrage_rows(
        db_path,
        filters={"player": "Mitchell"},
        active_profile=get_active_scanner_profile(db_path=str(db_path)),
    )

    assert [row["player"] for row in rows] == ["Mitchell Robinson"]


def test_no_active_profile_empty_state(tmp_path) -> None:
    db_path = tmp_path / "opportunities.db"
    get_active_scanner_profile(db_path=str(db_path))
    with sqlite3.connect(db_path) as conn:
        conn.execute("UPDATE scanner_profiles SET is_active = 0")

    assert get_active_scanner_profile(db_path=str(db_path)) is None
    assert live_opportunities_empty_message(None, []) == "No active scanner profile configured. Configure one in Scanner Config."


def test_run_active_profile_scan_command_is_allowlisted() -> None:
    assert ALLOWED_COMMANDS["scan-prop-arb"] == [
        "/home/noel/.venv/bin/python",
        "-m",
        "src.cli",
        "scan-prop-arb",
        "--profile",
        "__ACTIVE__",
        "--json",
    ]


def test_profile_mismatch_note_when_out_of_profile_opportunities_exist(tmp_path) -> None:
    db_path = tmp_path / "opportunities.db"
    create_scanner_profile(
        db_path=str(db_path),
        name="NBA",
        sport="basketball_nba",
        markets=["player_points"],
        is_active=True,
    )
    _save(db_path, player="In Profile")
    _save(db_path, player="Out Of Profile", prop_type="player_rebounds", line=21.5)

    note = profile_mismatch_note(db_path, get_scanner_profile_by_name("NBA", db_path=str(db_path)))

    assert note == "Other opportunities exist outside the active profile. View them in Opportunity Explorer."
