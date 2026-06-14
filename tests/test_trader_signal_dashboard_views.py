from __future__ import annotations

import inspect
import json
import sys
from datetime import datetime, timezone

from src.analysis import trader_signal_dashboard_views
from src.analysis.trader_signal_dashboard_views import (
    DASHBOARD_VIEWS,
    init_trader_signal_dashboard_views,
    trader_signal_dashboard_views_report,
)
from src.analysis.trader_signal_engine import (
    generate_signals_from_activity,
    generate_trader_signal_recommendations,
    persist_signals,
    score_trader_signals,
)
from src.analysis.trader_signal_paper_bridge import init_trader_signal_paper_bridge_db, run_trader_signal_paper_bridge
from src.analysis.trader_signal_validation import init_trader_signal_validation_db
from src.sqlite_utils import closing_connection

WALLET = "0x" + "b" * 40


def _event(action: str, **overrides):
    row = {
        "wallet": WALLET,
        "timestamp": 100,
        "event_type": action,
        "market_id": "market-1",
        "market_title": "Bitcoin Up or Down",
        "asset": "BTC",
        "side": "up",
        "action": action,
        "shares": 10,
        "price": 0.5,
        "amount": 500.0,
        "tx_hash": f"0x{action}",
    }
    row.update(overrides)
    return row


def seed_signal_family_validations(db_path, signal_type, *, count, accuracy):
    init_trader_signal_validation_db(db_path)
    correct = int(round(count * accuracy))
    resolved_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    with closing_connection(db_path) as conn:
        for index in range(count):
            conn.execute(
                """
                INSERT OR IGNORE INTO trader_signal_validation (
                    validation_key, recommendation_id, signal_id, market_id, signal_type,
                    recommendation_type, generated_at, resolved_at, outcome, predicted_side,
                    correct, roi_proxy, confidence, trader_address, validation_timestamp
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    f"seed:{signal_type}:{index}",
                    None,
                    f"sig-{signal_type}-{index}",
                    "market-1",
                    signal_type,
                    "paper_entry",
                    "2026-06-01T00:00:00Z",
                    resolved_at,
                    "win" if index < correct else "loss",
                    "up",
                    1 if index < correct else 0,
                    0.2,
                    72.0,
                    WALLET,
                    "2026-06-14T00:00:00Z",
                ),
            )


def _query_view(db_path, view_name: str):
    with closing_connection(db_path) as conn:
        return conn.execute(f"SELECT * FROM {view_name}").fetchall()


def test_empty_db_initialization(tmp_path):
    db_path = tmp_path / "empty.db"
    views_created = init_trader_signal_dashboard_views(db_path)

    assert views_created == list(DASHBOARD_VIEWS)
    with closing_connection(db_path) as conn:
        view_names = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'view' ORDER BY name"
            ).fetchall()
        }
    assert view_names == set(DASHBOARD_VIEWS)


def test_kpi_view_stability_on_empty_db(tmp_path):
    db_path = tmp_path / "empty.db"
    init_trader_signal_dashboard_views(db_path)

    rows = _query_view(db_path, "v_trader_signal_kpis")
    assert len(rows) == 1
    row = dict(rows[0])
    assert row["total_signals"] == 0
    assert row["validated_signals"] == 0
    assert row["proven_families"] == 0
    assert row["blocked_recommendations"] == 0
    assert row["paper_intents"] == 0
    assert row["read_only"] == 1
    assert row["paper_only"] == 1

    overview = dict(_query_view(db_path, "v_dashboard_overview")[0])
    assert overview["total_signals"] == 0
    assert overview["validation_accuracy"] == 0.0
    assert overview["simulated_paper_intents"] == 0
    assert overview["read_only"] == 1
    assert overview["paper_only"] == 1


def test_gate_view_stability_on_empty_db(tmp_path):
    db_path = tmp_path / "empty.db"
    init_trader_signal_dashboard_views(db_path)

    family_rows = [dict(row) for row in _query_view(db_path, "v_signal_family_performance")]
    assert len(family_rows) == 5
    assert all(row["validation_count"] == 0 for row in family_rows)
    assert all(row["gate_status"] == "unproven" for row in family_rows)

    summary = dict(_query_view(db_path, "v_gate_status_summary")[0])
    assert summary["proven_count"] == 0
    assert summary["unproven_count"] == 5
    assert summary["weak_count"] == 0
    assert summary["blocked_count"] == 5


def test_recommendation_pipeline_view(tmp_path):
    db_path = tmp_path / "signals.db"
    seed_signal_family_validations(db_path, "early_entry", count=20, accuracy=0.6)
    signals = generate_signals_from_activity([_event("buy")])
    persist_signals(signals, db_path=db_path)
    score_trader_signals(db_path=db_path)
    generate_trader_signal_recommendations(db_path=db_path, limit=10)
    init_trader_signal_dashboard_views(db_path)

    pipeline = dict(_query_view(db_path, "v_recommendation_pipeline")[0])
    assert pipeline["total_recommendations"] >= 1
    assert pipeline["promoted_count"] + pipeline["blocked_count"] == pipeline["total_recommendations"]


def test_paper_intent_pipeline_view(tmp_path):
    db_path = tmp_path / "signals.db"
    seed_signal_family_validations(db_path, "early_entry", count=20, accuracy=0.6)
    signals = generate_signals_from_activity([_event("buy")])
    persist_signals(signals, db_path=db_path)
    score_trader_signals(db_path=db_path)
    generate_trader_signal_recommendations(db_path=db_path, limit=10)
    init_trader_signal_paper_bridge_db(db_path)
    run_trader_signal_paper_bridge(db_path=db_path)
    init_trader_signal_dashboard_views(db_path)

    pipeline = dict(_query_view(db_path, "v_paper_intent_pipeline")[0])
    assert pipeline["total_intents"] >= 1
    assert (
        pipeline["candidate_count"] + pipeline["simulated_count"] + pipeline["blocked_count"]
        == pipeline["total_intents"]
    )


def test_trader_signal_dashboard_views_cli_json_output(capsys, monkeypatch, tmp_path):
    from src.cli import main

    db_path = tmp_path / "signals.db"
    expected = {
        "db_path": str(db_path),
        "views_created": list(DASHBOARD_VIEWS),
        "read_only": True,
        "paper_only": True,
    }

    def fake_report(**kwargs):
        return expected

    monkeypatch.setattr("src.cli.trader_signal_dashboard_views_report", fake_report)
    sys.argv = ["polylens", "trader-signal-dashboard-views", "--db-path", str(db_path), "--json"]
    main()
    output = json.loads(capsys.readouterr().out)
    assert output == expected


def test_dashboard_views_module_has_no_live_execution_references():
    source = inspect.getsource(trader_signal_dashboard_views)
    forbidden = [
        "src.trading",
        "KalshiExecutor",
        "submit_order",
        "place_order",
        "send_order",
        "private_key",
        "api_secret",
        "live_execution",
    ]
    assert all(token not in source for token in forbidden)


def test_trader_signal_dashboard_views_report_shape(tmp_path):
    db_path = tmp_path / "signals.db"
    result = trader_signal_dashboard_views_report(db_path=db_path)

    assert result["db_path"] == str(db_path)
    assert result["views_created"] == list(DASHBOARD_VIEWS)
    assert result["read_only"] is True
    assert result["paper_only"] is True


TREND_VIEWS = (
    "v_signal_performance_trend",
    "v_recommendation_trend",
    "v_intent_trend",
    "v_validation_trend",
)

TREND_SCHEMAS = {
    "v_signal_performance_trend": {"trend_date", "signal_type", "signal_count", "avg_score"},
    "v_recommendation_trend": {"trend_date", "recommendation_count", "blocked_count", "promoted_count"},
    "v_intent_trend": {
        "trend_date",
        "intent_count",
        "candidate_count",
        "simulated_count",
        "blocked_count",
        "total_notional_usd",
        "simulated_notional_usd",
    },
    "v_validation_trend": {
        "validation_date",
        "validation_count",
        "correct_count",
        "accuracy",
        "avg_roi_proxy",
    },
}


def test_trend_views_return_stable_schema_on_empty_db(tmp_path):
    db_path = tmp_path / "empty.db"
    init_trader_signal_dashboard_views(db_path)

    for view_name in TREND_VIEWS:
        with closing_connection(db_path) as conn:
            columns = {
                row[1]
                for row in conn.execute(f"PRAGMA table_info({view_name})").fetchall()
            }
        assert columns == TREND_SCHEMAS[view_name]


def test_gate_confidence_view_on_empty_db(tmp_path):
    db_path = tmp_path / "empty.db"
    init_trader_signal_dashboard_views(db_path)

    rows = [dict(row) for row in _query_view(db_path, "v_gate_confidence")]
    assert len(rows) == 5
    assert all(row["confidence_score"] == 0.0 for row in rows)
    assert all(row["gate_status"] == "unproven" for row in rows)


def test_database_health_view_on_empty_db(tmp_path):
    db_path = tmp_path / "empty.db"
    init_trader_signal_dashboard_views(db_path)

    health = dict(_query_view(db_path, "v_database_health")[0])
    assert health["signal_rows"] == 0
    assert health["validation_rows"] == 0
    assert health["recommendation_rows"] == 0
    assert health["intent_rows"] == 0
    assert health["read_only"] == 1
    assert health["paper_only"] == 1
