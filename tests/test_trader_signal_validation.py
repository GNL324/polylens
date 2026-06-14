from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from src.analysis.trader_signal_engine import (
    generate_signals_from_activity,
    generate_trader_signal_recommendations,
    init_trader_signal_db,
    persist_signals,
    score_trader_signals,
)
from src.analysis.trader_signal_validation import (
    init_trader_signal_validation_db,
    trader_signal_validation_report,
    validate_trader_signals,
    validate_trader_signals_from_path,
)
from src.sqlite_utils import closing_connection

FIXTURES = Path(__file__).resolve().parent / "fixtures"
WALLET = "0x" + "a" * 40
WALLET_B = "0x" + "b" * 40


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
        "amount": 250.0,
        "tx_hash": f"0x{action}",
    }
    row.update(overrides)
    return row


def _seed_signals_and_recommendations(db_path):
    persist_signals(generate_signals_from_activity([_event("buy", tx_hash="0xbuy1")]), db_path=db_path)
    score_trader_signals(db_path=db_path)
    generate_trader_signal_recommendations(db_path=db_path, limit=10)


def test_validation_persistence(tmp_path):
    db_path = tmp_path / "signals.db"
    _seed_signals_and_recommendations(db_path)
    outcomes = [
        {
            "outcome_key": "o1",
            "market_id": "market-1",
            "wallet": WALLET,
            "winning_side": "up",
            "outcome": "win",
            "resolved_at": "2026-06-14T20:00:00Z",
            "roi": 0.4,
        }
    ]

    result = validate_trader_signals(outcomes, db_path=db_path)

    assert result["read_only"] is True
    assert result["paper_only"] is True
    assert result["validations_inserted"] >= 1
    with closing_connection(db_path) as conn:
        count = conn.execute("SELECT COUNT(*) FROM trader_signal_validation").fetchone()[0]
    assert count >= 1


def test_idempotent_validation(tmp_path):
    db_path = tmp_path / "signals.db"
    _seed_signals_and_recommendations(db_path)
    outcomes = [
        {
            "outcome_key": "o1",
            "market_id": "market-1",
            "wallet": WALLET,
            "winning_side": "up",
            "outcome": "win",
            "resolved_at": "2026-06-14T20:00:00Z",
            "roi": 0.4,
        }
    ]

    first = validate_trader_signals(outcomes, db_path=db_path)
    second = validate_trader_signals(outcomes, db_path=db_path)

    assert first["validations_inserted"] >= 1
    assert second["validations_inserted"] == 0
    assert second["validations_skipped"] >= first["validations_inserted"]


def test_empty_db_behavior(tmp_path):
    db_path = tmp_path / "signals.db"

    result = validate_trader_signals([], db_path=db_path)
    report = trader_signal_validation_report(db_path=db_path)

    assert result["validations_inserted"] == 0
    assert report["total_validated"] == 0
    assert report["overall_accuracy"] is None
    assert report["read_only"] is True
    assert report["paper_only"] is True


def test_missing_outcomes_safe_with_existing_signals(tmp_path):
    db_path = tmp_path / "signals.db"
    _seed_signals_and_recommendations(db_path)

    result = validate_trader_signals([], db_path=db_path)

    assert result["validations_inserted"] == 0
    assert result["recommendations_seen"] >= 1


def test_accuracy_calculation(tmp_path):
    db_path = tmp_path / "signals.db"
    _seed_signals_and_recommendations(db_path)
    outcomes = [
        {
            "outcome_key": "win",
            "market_id": "market-1",
            "wallet": WALLET,
            "winning_side": "up",
            "outcome": "win",
            "resolved_at": "2026-06-14T20:00:00Z",
            "roi": 0.5,
        }
    ]

    validate_trader_signals(outcomes, db_path=db_path)
    report = trader_signal_validation_report(db_path=db_path)

    assert report["total_validated"] >= 1
    assert report["overall_accuracy"] == 1.0


def test_confidence_bucket_performance(tmp_path):
    db_path = tmp_path / "signals.db"
    _seed_signals_and_recommendations(db_path)
    validate_trader_signals(
        [
            {
                "outcome_key": "win",
                "market_id": "market-1",
                "wallet": WALLET,
                "winning_side": "up",
                "outcome": "win",
                "resolved_at": "2026-06-14T20:00:00Z",
                "roi": 0.5,
            }
        ],
        db_path=db_path,
    )

    report = trader_signal_validation_report(db_path=db_path)
    buckets = {row["bucket"]: row for row in report["confidence_bucket_performance"]}

    assert buckets
    assert any(row["count"] > 0 for row in report["confidence_bucket_performance"])


def test_recommendation_and_signal_performance_sections(tmp_path):
    db_path = tmp_path / "signals.db"
    _seed_signals_and_recommendations(db_path)
    validate_trader_signals(
        [
            {
                "outcome_key": "win",
                "market_id": "market-1",
                "wallet": WALLET,
                "winning_side": "up",
                "outcome": "win",
                "resolved_at": "2026-06-14T20:00:00Z",
                "roi": 0.5,
            }
        ],
        db_path=db_path,
    )

    report = trader_signal_validation_report(db_path=db_path)

    assert report["recommendation_type_performance"]
    assert report["signal_type_performance"]
    assert "count" in report["consensus_performance"]
    assert "count" in report["contrarian_performance"]


def test_report_json_shape_stability(tmp_path):
    db_path = tmp_path / "signals.db"
    report = trader_signal_validation_report(db_path=db_path)

    assert set(report.keys()) >= {
        "read_only",
        "paper_only",
        "total_validated",
        "overall_accuracy",
        "signal_type_performance",
        "recommendation_type_performance",
        "consensus_performance",
        "contrarian_performance",
        "confidence_bucket_performance",
        "top_traders",
        "worst_traders",
        "rolling_7_day",
        "rolling_30_day",
    }


def test_validate_from_fixture_path(tmp_path):
    db_path = tmp_path / "signals.db"
    persist_signals(
        generate_signals_from_activity(
            [
                _event(
                    "buy",
                    wallet="0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                    market_id="btc-updown-smoke-1",
                    tx_hash="0xbuy-smoke",
                )
            ]
        ),
        db_path=db_path,
    )
    score_trader_signals(db_path=db_path)
    generate_trader_signal_recommendations(db_path=db_path, limit=5)

    result = validate_trader_signals_from_path(
        FIXTURES / "trader_signal_validation_outcomes.json",
        db_path=db_path,
    )

    assert result["validations_inserted"] >= 1


def test_rolling_windows_use_resolved_at(tmp_path):
    db_path = tmp_path / "signals.db"
    init_trader_signal_validation_db(db_path)
    now = datetime(2026, 6, 14, tzinfo=timezone.utc)
    with closing_connection(db_path) as conn:
        conn.execute(
            """
            INSERT INTO trader_signal_validation (
                validation_key, recommendation_id, signal_id, market_id, signal_type,
                recommendation_type, generated_at, resolved_at, outcome, predicted_side,
                correct, roi_proxy, confidence, trader_address, validation_timestamp
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "manual:1",
                None,
                "sig-1",
                "market-1",
                "early_entry",
                None,
                "2026-06-01T00:00:00Z",
                "2026-06-13T12:00:00Z",
                "win",
                "up",
                1,
                0.2,
                72.0,
                WALLET,
                "2026-06-14T00:00:00Z",
            ),
        )

    report = trader_signal_validation_report(db_path=db_path, now=now)

    assert report["rolling_7_day"]["count"] == 1
    assert report["rolling_30_day"]["count"] == 1
