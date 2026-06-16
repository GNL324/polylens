from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.analysis.trader_signal_engine import (
    SIGNAL_TYPES,
    generate_signals_from_activity,
    generate_trader_signal_recommendations,
    persist_signals,
    score_trader_signals,
    trader_signal_report,
)
from src.analysis.trader_signal_gates import (
    DEFAULT_MINIMUM_ACCURACY,
    DEFAULT_MINIMUM_VALIDATIONS,
    GateThresholds,
    evaluate_signal_family_gate,
    load_signal_family_stats,
    trader_signal_gates_report,
)
from src.analysis.trader_signal_validation import backfill_trader_signal_validations, init_trader_signal_validation_db
from src.sqlite_utils import closing_connection

WALLET = "0x" + "a" * 40


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


def seed_signal_family_validations(
    db_path,
    signal_type: str,
    *,
    count: int,
    accuracy: float,
    resolved_at: str | None = None,
) -> None:
    init_trader_signal_validation_db(db_path)
    correct = int(round(count * accuracy))
    resolved_at = resolved_at or datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
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
                    f"market-{index % 3}",
                    signal_type,
                    "paper_entry",
                    "2026-06-01T00:00:00Z",
                    resolved_at,
                    "win" if index < correct else "loss",
                    "up",
                    1 if index < correct else 0,
                    0.2 if index < correct else -0.1,
                    72.0,
                    WALLET,
                    "2026-06-14T00:00:00Z",
                ),
            )


def test_insufficient_validation_count_blocks_gate():
    result = evaluate_signal_family_gate(validation_count=5, accuracy=1.0)

    assert result["gate_passes"] is False
    assert result["gate_status"] == "unproven"


def test_low_accuracy_blocks_gate():
    result = evaluate_signal_family_gate(validation_count=20, accuracy=0.5)

    assert result["gate_passes"] is False
    assert result["gate_status"] == "weak"


def test_passing_gate():
    result = evaluate_signal_family_gate(validation_count=20, accuracy=0.6)

    assert result["gate_passes"] is True
    assert result["gate_status"] == "proven"


def test_fixture_gate_scenarios(tmp_path):
    db_path = tmp_path / "signals.db"
    seed_signal_family_validations(db_path, "early_entry", count=20, accuracy=0.6)
    seed_signal_family_validations(db_path, "conviction", count=20, accuracy=0.5)
    seed_signal_family_validations(db_path, "exit", count=5, accuracy=1.0)

    stats = load_signal_family_stats(db_path=db_path)

    assert stats["early_entry"]["gate_status"] == "proven"
    assert stats["conviction"]["gate_status"] == "weak"
    assert stats["exit"]["gate_status"] == "unproven"


def test_gate_report_output(tmp_path):
    db_path = tmp_path / "signals.db"
    seed_signal_family_validations(db_path, "early_entry", count=20, accuracy=0.6)

    report = trader_signal_gates_report(db_path=db_path)

    assert report["read_only"] is True
    assert report["paper_only"] is True
    assert report["recommended_thresholds"]["minimum_validations"] == DEFAULT_MINIMUM_VALIDATIONS
    assert report["recommended_thresholds"]["minimum_accuracy"] == DEFAULT_MINIMUM_ACCURACY
    assert any(item["signal_type"] == "early_entry" for item in report["proven_signal_families"])
    assert report["gate_summary"]["proven_count"] >= 1


def test_recommendation_integration_blocks_unproven_family(tmp_path):
    db_path = tmp_path / "signals.db"
    persist_signals(generate_signals_from_activity([_event("buy", tx_hash="0xbuy1")]), db_path=db_path)
    score_trader_signals(db_path=db_path)

    result = generate_trader_signal_recommendations(db_path=db_path, limit=10)

    assert result["blocked_recommendations"]
    assert all(item["recommendation_type"] == "blocked" for item in result["blocked_recommendations"])
    assert all("validation_count" in item for item in result["recommendations"])
    assert all("gate_status" in item for item in result["recommendations"])


def test_recommendation_integration_promotes_proven_family(tmp_path):
    db_path = tmp_path / "signals.db"
    persist_signals(generate_signals_from_activity([_event("buy", tx_hash="0xbuy1", amount=500.0)]), db_path=db_path)
    score_trader_signals(db_path=db_path)
    for signal_type in ("early_entry", "conviction"):
        seed_signal_family_validations(db_path, signal_type, count=20, accuracy=0.6)

    result = generate_trader_signal_recommendations(db_path=db_path, limit=10)

    assert result["promoted_recommendations"]
    assert all(item["gate_status"] == "proven" for item in result["promoted_recommendations"])


def test_backfill_graduates_signal_family_gate(tmp_path):
    db_path = tmp_path / "signals.db"
    events = [
        _event("buy", timestamp=100 + index, tx_hash=f"0xbuy{index}", market_id=f"market-{index}", amount=500.0)
        for index in range(20)
    ]
    persist_signals(generate_signals_from_activity(events), db_path=db_path)
    score_trader_signals(db_path=db_path)

    def resolver(opportunity_id):
        return {
            "resolved": str(opportunity_id).startswith("market-"),
            "winner": "up",
            "settlement_price": 1.0,
            "settlement_timestamp": "2026-06-14T20:00:00Z",
            "source": "test",
        }

    before = trader_signal_gates_report(db_path=db_path)
    result = backfill_trader_signal_validations(db_path=db_path, resolver=resolver)
    after = trader_signal_gates_report(db_path=db_path)

    assert before["gate_summary"]["proven_count"] == 0
    assert result["validations_inserted"] >= 20
    assert after["gate_summary"]["proven_count"] >= 1


def test_report_includes_gate_sections(tmp_path):
    db_path = tmp_path / "signals.db"
    persist_signals(generate_signals_from_activity([_event("buy", tx_hash="0xbuy1")]), db_path=db_path)
    seed_signal_family_validations(db_path, "early_entry", count=20, accuracy=0.6)

    report = trader_signal_report(db_path=db_path, include_recommendations=True, recommendation_limit=5)

    assert "gate_summary" in report
    assert "promoted_recommendations" in report
    assert "blocked_recommendations" in report


def test_report_json_shape_stability(tmp_path):
    report = trader_signal_gates_report(db_path=tmp_path / "signals.db")

    assert set(report.keys()) >= {
        "read_only",
        "paper_only",
        "recommended_thresholds",
        "signal_families",
        "proven_signal_families",
        "blocked_signal_families",
        "gate_summary",
    }
    assert len(report["signal_families"]) == len(SIGNAL_TYPES)


def test_rolling_accuracy_in_family_stats(tmp_path):
    db_path = tmp_path / "signals.db"
    recent = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    old = (datetime.now(timezone.utc) - timedelta(days=40)).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    seed_signal_family_validations(db_path, "early_entry", count=20, accuracy=0.6, resolved_at=recent)

    stats = load_signal_family_stats(db_path=db_path)

    assert stats["early_entry"]["rolling_7_day_accuracy"] == 0.6
    assert stats["early_entry"]["rolling_30_day_accuracy"] == 0.6
    assert stats["early_entry"]["confidence_score"] > 0
