from __future__ import annotations

import sqlite3

from src.storage.opportunities import (
    archive_opportunity,
    bookmaker_pair_performance,
    create_paper_trade,
    get_prop_arbitrage_opportunity,
    load_paper_trades,
    latest_prop_scan_status,
    load_prop_arbitrage_opportunities,
    mark_opportunity_viewed,
    operations_metrics,
    opportunity_funnel,
    opportunity_timeline,
    paper_trade_summary,
    prop_arbitrage_summary_today,
    save_prop_arbitrage_scan_result,
    settle_paper_trade,
)
from src.web.dashboard import explorer_rows, paper_trade_rows, prop_arbitrage_rows


def _opportunity(**overrides):
    data = {
        "opportunity_type": "true_arbitrage",
        "player": "Mitchell Robinson",
        "prop_type": "player_points",
        "line": 2.5,
        "over_book": "BookA",
        "over_odds": 120,
        "under_book": "BookB",
        "under_odds": 110,
        "implied_probability_sum": 0.91,
        "guaranteed_roi": 0.098901,
        "guaranteed_profit_amount": 98.9,
        "ranking_score": 100.5,
        "stake_over": 520.0,
        "stake_under": 480.0,
    }
    data.update(overrides)
    return data


def test_prop_arbitrage_persistence(tmp_path) -> None:
    db_path = tmp_path / "opportunities.db"
    scan_run_id, opportunity_ids = save_prop_arbitrage_scan_result(
        {
            "props_fetched": 12,
            "matched_prop_pairs": 4,
            "rejected_pairs": 2,
            "scan_duration_seconds": 1.25,
            "prop_arbitrage_candidates": [_opportunity()],
        },
        db_path=str(db_path),
        sport="basketball_nba",
        markets="player_points",
        bankroll=1000,
        discovered_at="2026-06-05T10:00:00+00:00",
    )
    assert scan_run_id == 1
    assert opportunity_ids == [1]

    row = get_prop_arbitrage_opportunity(1, str(db_path))
    assert row is not None
    assert row["player"] == "Mitchell Robinson"
    assert row["sport"] == "basketball_nba"
    assert row["raw"]["stake_over"] == 520.0
    assert row["lifecycle_status"] == "discovered"


def test_prop_arbitrage_duplicate_suppression(tmp_path) -> None:
    db_path = tmp_path / "opportunities.db"
    result = {"prop_arbitrage_candidates": [_opportunity()]}
    _, first_ids = save_prop_arbitrage_scan_result(result, db_path=str(db_path), sport="basketball_nba", discovered_at="2026-06-05T10:00:00+00:00")
    _, duplicate_ids = save_prop_arbitrage_scan_result(result, db_path=str(db_path), sport="basketball_nba", discovered_at="2026-06-05T11:00:00+00:00")
    _, later_ids = save_prop_arbitrage_scan_result(result, db_path=str(db_path), sport="basketball_nba", discovered_at="2026-06-06T11:01:00+00:00")

    assert first_ids == [1]
    assert duplicate_ids == []
    assert later_ids == [2]


def test_dashboard_prop_queries_and_status(tmp_path) -> None:
    db_path = tmp_path / "opportunities.db"
    save_prop_arbitrage_scan_result(
        {
            "props_fetched": 20,
            "matched_prop_pairs": 5,
            "rejected_pairs": 3,
            "scan_duration_seconds": 2.5,
            "prop_arbitrage_candidates": [
                _opportunity(player="A", over_book="BookA", ranking_score=10, guaranteed_roi=0.03),
                _opportunity(player="B", over_book="BookC", ranking_score=20, guaranteed_roi=0.05),
            ],
        },
        db_path=str(db_path),
        sport="basketball_nba",
        discovered_at="2026-06-05T10:00:00+00:00",
    )

    rows = prop_arbitrage_rows(str(db_path), {"min_roi": 0.04, "sport": "basketball_nba", "bookmaker": "BookC", "player": "B"})
    assert [row["player"] for row in rows] == ["B"]
    assert rows[0]["roi_percent"] == "5.00%"

    status = latest_prop_scan_status(str(db_path))
    assert status is not None
    assert status["props_fetched"] == 20
    assert status["matched_pairs"] == 5
    assert status["rejected_pairs"] == 3
    assert status["opportunities_found"] == 2
    assert status["scan_duration"] == 2.5


def test_opportunity_detail_lookup_and_paper_trade_creation(tmp_path) -> None:
    db_path = tmp_path / "opportunities.db"
    _, ids = save_prop_arbitrage_scan_result({"prop_arbitrage_candidates": [_opportunity()]}, db_path=str(db_path), sport="basketball_nba")

    detail = get_prop_arbitrage_opportunity(ids[0], str(db_path))
    assert detail is not None
    assert detail["scan_run_id"] == 1
    viewed = mark_opportunity_viewed(ids[0], db_path=str(db_path), viewed_at="2026-06-05T10:01:00+00:00")
    assert viewed is not None
    assert viewed["lifecycle_status"] == "viewed"
    assert viewed["first_viewed_at"] == "2026-06-05T10:01:00+00:00"

    trade_id = create_paper_trade(ids[0], db_path=str(db_path), notes="test paper trade")
    assert trade_id == 1
    with sqlite3.connect(db_path) as conn:
        trade = conn.execute("SELECT opportunity_id, total_stake, expected_profit, expected_roi, result_status, status, notes FROM paper_trades").fetchone()
        status = conn.execute("SELECT status FROM prop_arbitrage_opportunities WHERE id = ?", (ids[0],)).fetchone()[0]
    assert trade == (ids[0], 1000.0, 98.9, 0.0989, "open", "open", "test paper trade")
    assert status == "paper"
    updated = get_prop_arbitrage_opportunity(ids[0], str(db_path))
    assert updated is not None
    assert updated["lifecycle_status"] == "paper_traded"
    assert updated["paper_trade_id"] == trade_id


def test_duplicate_paper_trade_prevention(tmp_path) -> None:
    db_path = tmp_path / "opportunities.db"
    _, ids = save_prop_arbitrage_scan_result({"prop_arbitrage_candidates": [_opportunity()]}, db_path=str(db_path), sport="basketball_nba")
    first = create_paper_trade(ids[0], db_path=str(db_path))
    second = create_paper_trade(ids[0], db_path=str(db_path))
    assert first == second
    assert len(load_paper_trades(db_path=str(db_path))) == 1


def test_settlement_over_wins(tmp_path) -> None:
    db_path = tmp_path / "opportunities.db"
    _, ids = save_prop_arbitrage_scan_result({"prop_arbitrage_candidates": [_opportunity()]}, db_path=str(db_path), sport="basketball_nba")
    trade_id = create_paper_trade(ids[0], db_path=str(db_path))
    settled = settle_paper_trade(trade_id, "over-won", db_path=str(db_path), settled_at="2026-06-05T12:00:00+00:00")
    assert settled["result_status"] == "won"
    assert settled["realized_profit"] == 144.0
    assert settled["settled_at"] == "2026-06-05T12:00:00+00:00"
    opportunity = get_prop_arbitrage_opportunity(ids[0], str(db_path))
    assert opportunity is not None
    assert opportunity["lifecycle_status"] == "settled"


def test_settlement_under_wins(tmp_path) -> None:
    db_path = tmp_path / "opportunities.db"
    _, ids = save_prop_arbitrage_scan_result({"prop_arbitrage_candidates": [_opportunity()]}, db_path=str(db_path), sport="basketball_nba")
    trade_id = create_paper_trade(ids[0], db_path=str(db_path))
    settled = settle_paper_trade(trade_id, "under-won", db_path=str(db_path))
    assert settled["result_status"] == "won"
    assert settled["realized_profit"] == 8.0


def test_push_void_settlement_and_manual_override(tmp_path) -> None:
    db_path = tmp_path / "opportunities.db"
    _, ids = save_prop_arbitrage_scan_result({"prop_arbitrage_candidates": [_opportunity()]}, db_path=str(db_path), sport="basketball_nba")
    trade_id = create_paper_trade(ids[0], db_path=str(db_path))
    pushed = settle_paper_trade(trade_id, "push", db_path=str(db_path))
    assert pushed["result_status"] == "push"
    assert pushed["realized_profit"] == 0.0
    overridden = settle_paper_trade(trade_id, "void", db_path=str(db_path), realized_profit=-3.5)
    assert overridden["result_status"] == "void"
    assert overridden["realized_profit"] == -3.5


def test_paper_trade_summary_calculations(tmp_path) -> None:
    db_path = tmp_path / "opportunities.db"
    _, first_ids = save_prop_arbitrage_scan_result({"prop_arbitrage_candidates": [_opportunity(player="One")]}, db_path=str(db_path), sport="basketball_nba")
    _, second_ids = save_prop_arbitrage_scan_result({"prop_arbitrage_candidates": [_opportunity(player="Two", line=3.5)]}, db_path=str(db_path), sport="basketball_nba")
    first_trade = create_paper_trade(first_ids[0], db_path=str(db_path))
    create_paper_trade(second_ids[0], db_path=str(db_path))
    settle_paper_trade(first_trade, "over-won", db_path=str(db_path), settled_at="2026-06-05T12:00:00+00:00")

    summary = paper_trade_summary(str(db_path))
    assert summary["open_paper_trades"] == 1
    assert summary["settled_paper_trades"] == 1
    assert summary["expected_open_profit"] == 98.9
    assert summary["realized_paper_pnl"] == 144.0
    assert summary["win_rate"] == 1
    assert summary["best_trade"] == 144.0
    assert summary["worst_trade"] == 144.0

    rows = paper_trade_rows(str(db_path))
    assert rows[0]["books"] == "BookA / BookB"
    assert "expected_roi_percent" in rows[0]


def test_paper_trade_schema_migration_compatibility(tmp_path) -> None:
    db_path = tmp_path / "opportunities.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE paper_trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                opportunity_id INTEGER,
                executed_at TEXT,
                stake REAL,
                expected_profit REAL,
                status TEXT,
                notes TEXT
            )
            """
        )
        conn.execute(
            "INSERT INTO paper_trades (opportunity_id, executed_at, stake, expected_profit, status, notes) VALUES (1, '2026-06-05T00:00:00+00:00', 25, 2.5, 'paper', 'legacy')"
        )
    rows = load_paper_trades(db_path=str(db_path))
    assert rows[0]["result_status"] == "paper"
    assert rows[0]["total_stake"] == 25


def test_archive_status_and_hidden_by_default(tmp_path) -> None:
    db_path = tmp_path / "opportunities.db"
    _, ids = save_prop_arbitrage_scan_result({"prop_arbitrage_candidates": [_opportunity()]}, db_path=str(db_path), sport="basketball_nba")
    archived = archive_opportunity(ids[0], db_path=str(db_path), archived_at="2026-06-05T11:00:00+00:00")
    assert archived is not None
    assert archived["lifecycle_status"] == "archived"
    assert archived["archived_at"] == "2026-06-05T11:00:00+00:00"
    assert load_prop_arbitrage_opportunities(db_path=str(db_path)) == []
    assert len(load_prop_arbitrage_opportunities(db_path=str(db_path), include_archived=True)) == 1


def test_opportunity_timeline_and_funnel(tmp_path) -> None:
    db_path = tmp_path / "opportunities.db"
    _, first_ids = save_prop_arbitrage_scan_result({"prop_arbitrage_candidates": [_opportunity(player="One")]}, db_path=str(db_path), sport="basketball_nba", discovered_at="2026-06-05T10:00:00+00:00")
    _, second_ids = save_prop_arbitrage_scan_result({"prop_arbitrage_candidates": [_opportunity(player="Two", line=3.5)]}, db_path=str(db_path), sport="basketball_nba", discovered_at="2026-06-05T10:00:00+00:00")
    mark_opportunity_viewed(first_ids[0], db_path=str(db_path), viewed_at="2026-06-05T10:05:00+00:00")
    trade_id = create_paper_trade(first_ids[0], db_path=str(db_path), executed_at="2026-06-05T10:10:00+00:00")
    settle_paper_trade(trade_id, "push", db_path=str(db_path), settled_at="2026-06-05T10:20:00+00:00")
    archive_opportunity(second_ids[0], db_path=str(db_path), archived_at="2026-06-05T10:30:00+00:00")

    timeline = opportunity_timeline(first_ids[0], str(db_path))
    assert timeline == [
        {"event": "discovered", "timestamp": "2026-06-05T10:00:00+00:00"},
        {"event": "viewed", "timestamp": "2026-06-05T10:05:00+00:00"},
        {"event": "paper_traded", "timestamp": "2026-06-05T10:10:00+00:00"},
        {"event": "settled", "timestamp": "2026-06-05T10:20:00+00:00"},
        {"event": "archived", "timestamp": None},
    ]
    funnel = {row["lifecycle_status"]: row for row in opportunity_funnel(str(db_path))}
    assert funnel["settled"]["count"] == 1
    assert funnel["archived"]["count"] == 1
    assert funnel["settled"]["conversion_percent"] == 0.5


def test_operations_metrics_and_bookmaker_performance(tmp_path) -> None:
    db_path = tmp_path / "opportunities.db"
    save_prop_arbitrage_scan_result(
        {
            "props_fetched": 20,
            "matched_prop_pairs": 5,
            "rejected_pairs": 3,
            "scan_duration_seconds": 2.5,
            "rejection_reasons": {"total implied probability >= 1": 3},
            "prop_arbitrage_candidates": [_opportunity(over_book="BetRivers", under_book="Bovada")],
        },
        db_path=str(db_path),
        sport="basketball_nba",
        discovered_at="2026-06-05T10:00:00+00:00",
    )
    opp = load_prop_arbitrage_opportunities(db_path=str(db_path))[0]
    trade_id = create_paper_trade(opp["id"], db_path=str(db_path), executed_at="2026-06-05T10:10:00+00:00")
    settle_paper_trade(trade_id, "push", db_path=str(db_path), settled_at="2026-06-05T10:20:00+00:00")

    metrics = operations_metrics(str(db_path), now=__import__("datetime").datetime(2026, 6, 5, 12, 0, tzinfo=__import__("datetime").timezone.utc))
    assert metrics["last_scan_time"] == "2026-06-05T10:00:00+00:00"
    assert metrics["scan_duration"] == 2.5
    assert metrics["scans_today"] == 1
    assert metrics["opportunities_today"] == 1
    assert metrics["paper_trades_today"] == 1
    assert metrics["settled_trades_today"] == 1
    assert metrics["rejection_reasons"] == {"total implied probability >= 1": 3}
    assert metrics["database_size"] > 0

    performance = bookmaker_pair_performance(str(db_path))
    assert performance[0]["book_pair"] == "BetRivers <-> Bovada"
    assert performance[0]["opportunities_found"] == 1
    assert performance[0]["settled_trade_count"] == 1


def test_explorer_filters(tmp_path) -> None:
    db_path = tmp_path / "opportunities.db"
    save_prop_arbitrage_scan_result(
        {
            "prop_arbitrage_candidates": [
                _opportunity(player="Mitchell Robinson", prop_type="player_points", over_book="BetRivers", guaranteed_roi=0.05, guaranteed_profit_amount=50),
                _opportunity(player="Jalen Brunson", prop_type="player_assists", over_book="FanDuel", line=6.5, guaranteed_roi=0.02, guaranteed_profit_amount=20),
            ]
        },
        db_path=str(db_path),
        sport="basketball_nba",
        discovered_at="2026-06-05T10:00:00+00:00",
    )
    rows = explorer_rows(
        str(db_path),
        {
            "player": "Mitchell",
            "prop_type": "player_points",
            "bookmaker": "BetRivers",
            "min_roi": 0.04,
            "max_roi": 0.06,
            "date_from": "2026-06-05",
            "date_to": "2026-06-06",
            "sort_by": "roi",
        },
    )
    assert [row["player"] for row in rows] == ["Mitchell Robinson"]


def test_prop_summary_today(tmp_path) -> None:
    db_path = tmp_path / "opportunities.db"
    save_prop_arbitrage_scan_result(
        {"prop_arbitrage_candidates": [_opportunity(guaranteed_roi=0.04, guaranteed_profit_amount=40), _opportunity(player="Other", guaranteed_roi=0.08, guaranteed_profit_amount=80)]},
        db_path=str(db_path),
        sport="basketball_nba",
        discovered_at="2026-06-05T10:00:00+00:00",
    )
    summary = prop_arbitrage_summary_today(str(db_path), now=__import__("datetime").datetime(2026, 6, 5, 12, 0, tzinfo=__import__("datetime").timezone.utc))
    assert summary["open_opportunities"] == 2
    assert summary["opportunities_found_today"] == 2
    assert summary["highest_roi_today"] == 0.08
    assert summary["average_roi_today"] == 0.06
    assert summary["potential_profit_today"] == 120
