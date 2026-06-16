from __future__ import annotations

import inspect
import json
import sys
from datetime import datetime, timezone

from src.analysis import trader_signal_paper_bridge
from src.analysis.trader_signal_engine import (
    generate_signals_from_activity,
    generate_trader_signal_recommendations,
    persist_signals,
    score_trader_signals,
    trader_signal_report,
)
from src.analysis.trader_signal_paper_bridge import (
    PaperBridgeConfig,
    build_paper_intent_from_recommendation,
    init_trader_signal_paper_bridge_db,
    run_trader_signal_paper_bridge,
    trader_signal_paper_bridge_report,
)
from src.analysis.trader_signal_validation import backfill_trader_signal_validations, init_trader_signal_validation_db
from src.sqlite_utils import closing_connection

WALLET = "0x" + "a" * 40


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


def _seed_recommendations(db_path, *, amount=500.0, signal_types=("early_entry", "conviction")):
    persist_signals(generate_signals_from_activity([_event("buy", tx_hash="0xbuy1", amount=amount)]), db_path=db_path)
    score_trader_signals(db_path=db_path)
    for signal_type in signal_types:
        seed_signal_family_validations(db_path, signal_type, count=20, accuracy=0.6)
    generate_trader_signal_recommendations(db_path=db_path, limit=10)


def test_unproven_recommendations_blocked(tmp_path):
    db_path = tmp_path / "signals.db"
    persist_signals(generate_signals_from_activity([_event("buy", tx_hash="0xbuy1")]), db_path=db_path)
    score_trader_signals(db_path=db_path)
    generate_trader_signal_recommendations(db_path=db_path, limit=10)

    result = run_trader_signal_paper_bridge(db_path=db_path)

    assert result["read_only"] is True
    assert result["paper_only"] is True
    assert result["blocked_count"] >= 1
    assert all(intent["status"] == "blocked" for intent in result["intents"])


def test_weak_recommendations_blocked(tmp_path):
    db_path = tmp_path / "signals.db"
    persist_signals(generate_signals_from_activity([_event("buy", tx_hash="0xbuy1")]), db_path=db_path)
    score_trader_signals(db_path=db_path)
    seed_signal_family_validations(db_path, "early_entry", count=20, accuracy=0.5)
    seed_signal_family_validations(db_path, "conviction", count=20, accuracy=0.5)
    generate_trader_signal_recommendations(db_path=db_path, limit=10)

    result = run_trader_signal_paper_bridge(db_path=db_path)

    assert result["blocked_count"] >= 1
    assert result["simulated_count"] == 0


def test_proven_recommendations_create_intent(tmp_path):
    db_path = tmp_path / "signals.db"
    _seed_recommendations(db_path)

    result = run_trader_signal_paper_bridge(db_path=db_path)

    assert result["simulated_count"] >= 1 or result["candidate_count"] >= 1
    assert any(intent["gate_status"] == "proven" for intent in result["intents"])
    assert any(intent["status"] in {"candidate", "simulated"} for intent in result["intents"])


def test_paper_bridge_runs_after_backfilled_validation(tmp_path):
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

    backfill_trader_signal_validations(db_path=db_path, resolver=resolver)
    generate_trader_signal_recommendations(db_path=db_path, limit=10)

    result = run_trader_signal_paper_bridge(db_path=db_path)

    assert any(intent["gate_status"] == "proven" for intent in result["intents"])
    assert result["simulated_count"] >= 1 or result["candidate_count"] >= 1


def test_mixed_families_only_proven_reach_simulation(tmp_path):
    db_path = tmp_path / "signals.db"
    events = [
        _event(
            "buy",
            timestamp=100 + index,
            tx_hash=f"0xbuy{index}",
            market_id=f"market-{index}",
            price=0.1,
            amount=500.0,
        )
        for index in range(25)
    ]
    persist_signals(generate_signals_from_activity(events), db_path=db_path)
    score_trader_signals(db_path=db_path)
    seed_signal_family_validations(db_path, "early_entry", count=20, accuracy=0.6)
    seed_signal_family_validations(db_path, "conviction", count=20, accuracy=0.5)

    recommendations = generate_trader_signal_recommendations(db_path=db_path, limit=20)
    result = run_trader_signal_paper_bridge(db_path=db_path, config=PaperBridgeConfig(limit=20))

    selected_distribution = {
        row["signal_type"]: row for row in recommendations["recommendation_distribution"]
    }
    preselection_distribution = {
        row["signal_type"]: row for row in recommendations["preselection_recommendation_distribution"]
    }

    assert preselection_distribution["conviction"]["weak"] >= 20
    assert selected_distribution["early_entry"]["proven"] >= 1
    assert result["simulated_count"] >= 1
    assert any(intent["signal_type"] == "early_entry" and intent["status"] == "simulated" for intent in result["intents"])
    assert all(
        intent["status"] == "blocked"
        for intent in result["intents"]
        if intent["signal_type"] == "conviction"
    )


def test_low_score_blocked(tmp_path):
    intent = build_paper_intent_from_recommendation(
        {
            "recommendation_key": "rec-1",
            "wallet": WALLET,
            "market_id": "market-1",
            "side": "up",
            "recommendation_type": "paper_entry",
            "signal_type": "early_entry",
            "score": 55.0,
            "validation_count": 20,
            "historical_accuracy": 0.6,
            "gate_status": "proven",
            "gate_reason": "ok",
        },
        config=PaperBridgeConfig(minimum_score=60.0),
    )

    assert intent["status"] == "blocked"
    assert "score" in intent["reason"]


def test_idempotent_persistence(tmp_path):
    db_path = tmp_path / "signals.db"
    _seed_recommendations(db_path)

    first = run_trader_signal_paper_bridge(db_path=db_path)
    second = run_trader_signal_paper_bridge(db_path=db_path)

    assert first["intents_created"] >= 1
    assert second["intents_created"] == 0
    assert second["intents_skipped"] >= first["intents_created"]
    with closing_connection(db_path) as conn:
        count = conn.execute("SELECT COUNT(*) FROM trader_signal_paper_intents").fetchone()[0]
    assert count == first["intents_created"]


def test_empty_db_behavior(tmp_path):
    db_path = tmp_path / "signals.db"

    result = run_trader_signal_paper_bridge(db_path=db_path)
    report = trader_signal_paper_bridge_report(db_path=db_path)

    assert result["intents_created"] == 0
    assert result["intents"] == []
    assert report["total_intents"] == 0


def test_report_json_shape(tmp_path):
    db_path = tmp_path / "signals.db"
    _seed_recommendations(db_path)
    run_trader_signal_paper_bridge(db_path=db_path)

    report = trader_signal_paper_bridge_report(db_path=db_path)

    assert set(report.keys()) >= {
        "read_only",
        "paper_only",
        "total_intents",
        "blocked",
        "candidates",
        "simulated",
        "by_signal_type",
        "by_trader",
        "average_score",
        "average_historical_accuracy",
        "latest_intents",
    }


def test_trader_signal_report_includes_paper_bridge_summary(tmp_path):
    db_path = tmp_path / "signals.db"
    _seed_recommendations(db_path)
    run_trader_signal_paper_bridge(db_path=db_path)

    report = trader_signal_report(
        db_path=db_path,
        include_recommendations=True,
        include_paper_bridge_summary=True,
        recommendation_limit=5,
    )

    assert "paper_bridge_summary" in report
    assert report["paper_bridge_summary"]["total_intents"] >= 1


def test_trader_signal_paper_bridge_cli_json_output(capsys, monkeypatch, tmp_path):
    from src.cli import main

    db_path = tmp_path / "signals.db"
    expected = {"read_only": True, "paper_only": True, "intents_created": 0, "intents": []}

    def fake_bridge(**kwargs):
        return expected

    monkeypatch.setattr("src.cli.run_trader_signal_paper_bridge", fake_bridge)
    sys.argv = ["polylens", "trader-signal-paper-bridge", "--db-path", str(db_path), "--json"]
    main()
    output = json.loads(capsys.readouterr().out)
    assert output == expected


def test_paper_bridge_module_has_no_live_execution_imports_or_calls():
    source = inspect.getsource(trader_signal_paper_bridge)
    forbidden = [
        "src.trading",
        "KalshiExecutor",
        "submit_order",
        "place_order",
        "send_order",
        "private_key",
        "api_secret",
    ]
    assert all(token not in source for token in forbidden)


def test_fixture_gate_scenarios_for_bridge(tmp_path):
    db_path = tmp_path / "signals.db"
    init_trader_signal_paper_bridge_db(db_path)

    passing = build_paper_intent_from_recommendation(
        {
            "recommendation_key": "pass",
            "wallet": WALLET,
            "market_id": "market-1",
            "side": "up",
            "recommendation_type": "paper_entry",
            "signal_type": "early_entry",
            "score": 75.0,
            "validation_count": 20,
            "historical_accuracy": 0.6,
            "gate_status": "proven",
            "gate_reason": "ok",
        }
    )
    weak = build_paper_intent_from_recommendation(
        {
            "recommendation_key": "weak",
            "wallet": WALLET,
            "market_id": "market-1",
            "side": "up",
            "recommendation_type": "paper_entry",
            "signal_type": "conviction",
            "score": 75.0,
            "validation_count": 20,
            "historical_accuracy": 0.5,
            "gate_status": "weak",
            "gate_reason": "low accuracy",
        }
    )
    unproven = build_paper_intent_from_recommendation(
        {
            "recommendation_key": "unproven",
            "wallet": WALLET,
            "market_id": "market-1",
            "side": "up",
            "recommendation_type": "paper_entry",
            "signal_type": "exit",
            "score": 75.0,
            "validation_count": 5,
            "historical_accuracy": 1.0,
            "gate_status": "unproven",
            "gate_reason": "insufficient validations",
        }
    )

    assert passing["status"] == "simulated"
    assert weak["status"] == "blocked"
    assert unproven["status"] == "blocked"
