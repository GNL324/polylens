from unittest.mock import patch

from src.storage.opportunity_store import OpportunityStore


def candidate():
    return {
        "venue_pair": "polymarket_sportsbook",
        "polymarket_id": "pm1",
        "polymarket_title": "Will Lakers win?",
        "sportsbook_event_id": "evt1",
        "sportsbook": "Book",
        "sportsbook_team": "Los Angeles Lakers",
        "raw_edge": 0.05,
        "estimated_edge": 0.05,
        "execution_score": 0.8,
        "score_reason": "edge available",
        "polymarket_implied_yes_price": 0.45,
        "sportsbook_implied_probability": 0.6,
        "commence_time": "2026-06-10T00:00:00Z",
    }


def test_database_initialization(tmp_path):
    db = tmp_path / "custom.db"
    store = OpportunityStore(db)
    assert db.exists()
    stats = store.stats()
    assert stats["scan_runs"] == 0


def test_saving_scan_run(tmp_path):
    store = OpportunityStore(tmp_path / "polylens.db")
    run_id = store.save_scan_run("scan-live-arb", filters={"keyword": "btc"}, venues_scanned={"polymarket": 1}, candidate_count=1)
    assert run_id == 1
    assert store.stats()["scan_runs"] == 1


def test_saving_opportunity(tmp_path):
    store = OpportunityStore(tmp_path / "polylens.db")
    run_id = store.save_scan_run("scan-live-arb")
    opp_id = store.save_opportunity(candidate(), scan_run_id=run_id)
    rows = store.recent_opportunities()
    assert opp_id == rows[0]["id"]
    assert rows[0]["market_titles"]["polymarket"] == "Will Lakers win?"
    assert rows[0]["execution_score"] == 0.8


def test_saving_alert(tmp_path):
    store = OpportunityStore(tmp_path / "polylens.db")
    run_id = store.save_scan_run("watch-live-arb")
    opp_id = store.save_opportunity(candidate(), scan_run_id=run_id)
    alert_id = store.save_alert(opp_id, scan_run_id=run_id, destination="console", status="sent", payload={"title": "test"})
    rows = store.recent_alerts()
    assert alert_id == rows[0]["id"]
    assert rows[0]["destination"] == "console"


def test_saving_rejected_candidate(tmp_path):
    store = OpportunityStore(tmp_path / "polylens.db")
    run_id = store.save_scan_run("explain-matches")
    rejected_id = store.save_rejected_candidate({"polymarket_title": "BTC up", "kalshi_title": "ETH up", "reason": "asset mismatch", "parsed_polymarket": {"asset": "BTC"}}, scan_run_id=run_id)
    assert rejected_id == 1
    assert store.stats()["rejected_candidates"] == 1


def test_db_path_uses_custom_path(tmp_path):
    db = tmp_path / "nested" / "custom.db"
    OpportunityStore(db).save_scan_run("scan-live-arb")
    assert db.exists()


def test_recent_opportunities_cli(tmp_path, capsys):
    from src.cli import recent_opportunities

    db = tmp_path / "polylens.db"
    store = OpportunityStore(db)
    store.save_opportunity(candidate())
    rows = recent_opportunities(limit=5, db_path=str(db))
    assert rows
    assert "Will Lakers win" in capsys.readouterr().out


def test_recent_alerts_cli(tmp_path, capsys):
    from src.cli import recent_alerts

    db = tmp_path / "polylens.db"
    store = OpportunityStore(db)
    store.save_alert(None, destination="console", status="sent", payload={"title": "alert"})
    rows = recent_alerts(limit=5, db_path=str(db))
    assert rows[0]["status"] == "sent"
    assert "console" in capsys.readouterr().out


def test_opportunity_stats_cli(tmp_path, capsys):
    from src.cli import opportunity_stats

    db = tmp_path / "polylens.db"
    store = OpportunityStore(db)
    store.save_scan_run("scan-live-arb")
    stats = opportunity_stats(db_path=str(db))
    assert stats["scan_runs"] == 1
    assert "Scan runs" in capsys.readouterr().out


@patch("src.cli.PolymarketClient")
@patch("src.cli.KalshiClient")
def test_scan_live_arb_save_writes_db(mock_kalshi, mock_poly, tmp_path):
    from src.cli import scan_live_arb

    db = tmp_path / "polylens.db"
    mock_poly.return_value.get_active_markets.return_value = []
    mock_kalshi.return_value.get_markets.return_value = []
    result = scan_live_arb(save=True, db_path=str(db), as_json=True)
    assert result["saved_scan_run_id"] == 1
    assert OpportunityStore(db).stats()["scan_runs"] == 1


@patch("src.analysis.watch_mode.run_live_scan")
def test_watch_live_arb_once_save_writes_scan_and_alert(mock_scan, tmp_path):
    from src.alerts.notifier import ConsoleNotifier
    from src.analysis.watch_mode import watch_live_arbitrage

    db = tmp_path / "polylens.db"
    mock_scan.return_value = {"top_candidates": [candidate()], "top_scored_candidates": [candidate()], "candidates_after_filtering": 1, "markets_scanned_by_venue": {"polymarket": 1}}
    result = watch_live_arbitrage(ConsoleNotifier(), once=True, as_json=True, save=True, db_path=str(db))
    stats = OpportunityStore(db).stats()
    assert result["alerts_sent"] == 1
    assert stats["scan_runs"] >= 1
    assert stats["opportunities"] == 1
    assert stats["alerts"] == 1
