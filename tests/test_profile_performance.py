from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from src.storage.opportunities import (
    active_profile_operations_metrics,
    book_pair_by_profile_performance,
    create_paper_trade,
    create_scanner_profile,
    create_scanner_profile_from_template,
    get_active_scanner_profile,
    get_scanner_profile,
    profile_performance,
    record_alert_event,
    save_prop_arbitrage_scan_result,
    settle_paper_trade,
    template_performance,
)
from src.web.dashboard import profile_performance_rows, scanner_profile_rows
from src.web.profile_templates import get_scanner_profile_template


def _candidate(player: str = "Player A", **overrides):
    row = {
        "player": player,
        "prop_type": "player_points",
        "line": 20.5,
        "over_book": "fanduel",
        "over_odds": 120,
        "under_book": "bovada",
        "under_odds": -105,
        "implied_probability_sum": 0.96,
        "guaranteed_roi": 0.04,
        "guaranteed_profit_amount": 12.0,
        "ranking_score": 95,
        "stake_over": 50,
        "stake_under": 50,
    }
    row.update(overrides)
    return row


def _profiled_scan(db_path, profile, *, discovered_at="2026-06-05T10:00:00+00:00"):
    return save_prop_arbitrage_scan_result(
        {
            "props_fetched": 10,
            "matched_prop_pairs": 2,
            "rejected_pairs": 1,
            "scan_duration_seconds": 1.2,
            "prop_arbitrage_candidates": [
                _candidate("Player A"),
                _candidate("Player B", line=21.5, guaranteed_roi=0.08, guaranteed_profit_amount=20.0),
            ],
            "rejection_reasons": {},
        },
        db_path=str(db_path),
        sport=profile["sport"],
        markets=",".join(profile["markets"]),
        bankroll=profile["bankroll"],
        discovered_at=discovered_at,
        finished_at=discovered_at,
        profile_id=profile["id"],
        profile_name=profile["name"],
        template_id=profile.get("template_id"),
    )


def test_scan_run_and_opportunities_store_profile_metadata(tmp_path) -> None:
    db_path = tmp_path / "opportunities.db"
    profile_id = create_scanner_profile(db_path=str(db_path), name="NBA Points", sport="basketball_nba", markets=["player_points"], is_active=True)
    profile = get_scanner_profile(profile_id, db_path=str(db_path))

    scan_run_id, opportunity_ids = _profiled_scan(db_path, profile)

    with sqlite3.connect(db_path) as conn:
        scan = conn.execute("SELECT profile_id, profile_name, template_id FROM prop_arbitrage_scan_runs WHERE id = ?", (scan_run_id,)).fetchone()
        opportunity = conn.execute("SELECT profile_id, profile_name, template_id FROM prop_arbitrage_opportunities WHERE id = ?", (opportunity_ids[0],)).fetchone()
    assert scan == (profile_id, "NBA Points", None)
    assert opportunity == (profile_id, "NBA Points", None)


def test_profile_performance_calculations(tmp_path) -> None:
    db_path = tmp_path / "opportunities.db"
    profile_id = create_scanner_profile(db_path=str(db_path), name="NBA Points", sport="basketball_nba", markets=["player_points"], is_active=True)
    profile = get_scanner_profile(profile_id, db_path=str(db_path))
    _, opportunity_ids = _profiled_scan(db_path, profile)
    trade_id = create_paper_trade(opportunity_ids[0], db_path=str(db_path))
    settle_paper_trade(trade_id, "over-won", db_path=str(db_path), realized_profit=9.5)
    record_alert_event(db_path=str(db_path), opportunity_id=opportunity_ids[0], status="sent")
    record_alert_event(db_path=str(db_path), opportunity_id=opportunity_ids[0], status="duplicate")
    record_alert_event(db_path=str(db_path), opportunity_id=opportunity_ids[0], status="failed")

    row = next(item for item in profile_performance(str(db_path)) if item["profile_id"] == profile_id)

    assert row["scan_count"] == 1
    assert row["opportunities_found"] == 2
    assert row["average_roi"] == 0.06
    assert row["max_roi"] == 0.08
    assert row["total_expected_profit"] == 32.0
    assert row["paper_trades_created"] == 1
    assert row["settled_paper_trades"] == 1
    assert row["realized_pnl"] == 9.5
    assert row["alert_count"] == 3
    assert row["duplicate_alert_count"] == 1
    assert row["failed_alert_count"] == 1


def test_template_and_book_pair_aggregation(tmp_path) -> None:
    db_path = tmp_path / "opportunities.db"
    template = get_scanner_profile_template("nba_points_all")
    profile_id = create_scanner_profile_from_template(template, db_path=str(db_path), activate=True)
    profile = get_scanner_profile(profile_id, db_path=str(db_path))
    _, opportunity_ids = _profiled_scan(db_path, profile)
    trade_id = create_paper_trade(opportunity_ids[0], db_path=str(db_path))
    settle_paper_trade(trade_id, "over-won", db_path=str(db_path), realized_profit=7.25)

    template_row = next(row for row in template_performance(str(db_path)) if row["template_id"] == "nba_points_all")
    pair_row = next(row for row in book_pair_by_profile_performance(str(db_path)) if row["profile"] == profile["name"])

    assert template_row["template_name"] == "NBA Points - All Books"
    assert template_row["profiles_created"] == 1
    assert template_row["scans"] == 1
    assert template_row["opportunities"] == 2
    assert template_row["paper_trades"] == 1
    assert template_row["realized_pnl"] == 7.25
    assert pair_row["book_pair"] == "fanduel <-> bovada"
    assert pair_row["opportunities"] == 2
    assert pair_row["paper_trades"] == 1
    assert pair_row["realized_pnl"] == 7.25


def test_scanner_config_performance_badges_and_dashboard_rows(tmp_path) -> None:
    db_path = tmp_path / "opportunities.db"
    profile_id = create_scanner_profile(db_path=str(db_path), name="NBA Points", sport="basketball_nba", markets=["player_points"], is_active=True)
    profile = get_scanner_profile(profile_id, db_path=str(db_path))
    _profiled_scan(db_path, profile)

    config_row = next(row for row in scanner_profile_rows(str(db_path)) if row["id"] == profile_id)
    perf_row = next(row for row in profile_performance_rows(str(db_path)) if row["profile_id"] == profile_id)

    assert config_row["scans"] == 1
    assert config_row["opps"] == 2
    assert config_row["avg_roi"] == "6.00%"
    assert perf_row["average_roi"] == "6.00%"


def test_active_profile_operations_metrics(tmp_path) -> None:
    db_path = tmp_path / "opportunities.db"
    profile_id = create_scanner_profile(db_path=str(db_path), name="NBA Points", sport="basketball_nba", markets=["player_points"], is_active=True)
    profile = get_active_scanner_profile(db_path=str(db_path))
    _, opportunity_ids = _profiled_scan(db_path, profile, discovered_at="2026-06-05T12:00:00+00:00")
    record_alert_event(db_path=str(db_path), opportunity_id=opportunity_ids[0], status="sent", created_at="2026-06-05T12:05:00+00:00")

    metrics = active_profile_operations_metrics(str(db_path), now=datetime(2026, 6, 5, 13, tzinfo=timezone.utc))

    assert metrics["active_profile"]["id"] == profile_id
    assert metrics["scans_today"] == 1
    assert metrics["opportunities_today"] == 2
    assert metrics["alerts_today"] == 1
    assert metrics["summary"]["scan_count"] == 1


def test_old_rows_with_null_profile_data_do_not_break_queries(tmp_path) -> None:
    db_path = tmp_path / "opportunities.db"
    save_prop_arbitrage_scan_result(
        {
            "props_fetched": 1,
            "matched_prop_pairs": 1,
            "rejected_pairs": 0,
            "scan_duration_seconds": 0.1,
            "prop_arbitrage_candidates": [_candidate("Old Row")],
            "rejection_reasons": {},
        },
        db_path=str(db_path),
        sport="basketball_nba",
        markets="player_points",
    )

    assert profile_performance(str(db_path))
    assert template_performance(str(db_path)) == []
    assert book_pair_by_profile_performance(str(db_path)) == []
