from __future__ import annotations

from src.analysis.trader_signal_engine import (
    generate_signals_from_activity,
    generate_trader_signal_recommendations,
    persist_signals,
    score_trader_signals,
)
from src.analysis.trader_signal_validation import init_trader_signal_validation_db
from src.sqlite_utils import closing_connection

WALLET = "0x" + "a" * 40


def _seed_passing_validations(db_path, signal_types):
    init_trader_signal_validation_db(db_path)
    with closing_connection(db_path) as conn:
        for signal_type in signal_types:
            for index in range(20):
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
                        "2026-06-14T12:00:00Z",
                        "win" if index < 12 else "loss",
                        "up",
                        1 if index < 12 else 0,
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
        "amount": 5.0,
        "tx_hash": f"0x{action}",
    }
    row.update(overrides)
    return row


def test_generate_recommendations_from_scored_signals(tmp_path):
    db_path = tmp_path / "signals.db"
    events = [_event("buy", tx_hash="0xbuy1", amount=500.0)]
    persist_signals(generate_signals_from_activity(events), db_path=db_path)
    score_trader_signals(db_path=db_path)
    for signal_type in ("early_entry", "conviction"):
        _seed_passing_validations(db_path, (signal_type,))

    result = generate_trader_signal_recommendations(db_path=db_path, limit=10)

    assert result["read_only"] is True
    assert result["paper_only"] is True
    assert result["recommendation_count"] >= 1
    assert all(
        item["recommendation_type"] in {"watch", "paper_entry", "paper_exit", "avoid", "blocked"}
        for item in result["recommendations"]
    )
    assert all("gate_status" in item for item in result["recommendations"])


def test_conflict_handling_prefers_avoid(tmp_path):
    db_path = tmp_path / "signals.db"
    events = [
        _event("buy", tx_hash="0xbuy1", amount=500.0, price=0.2),
        _event("sell", timestamp=200, tx_hash="0xsell1", price=0.8, amount=500.0),
    ]
    persist_signals(generate_signals_from_activity(events), db_path=db_path)
    score_trader_signals(db_path=db_path)
    _seed_passing_validations(db_path, ("early_entry", "conviction", "exit"))

    result = generate_trader_signal_recommendations(db_path=db_path, limit=20)

    assert result["conflicts"] >= 1
    assert any(item["recommendation_type"] == "avoid" and item["conflict"] for item in result["recommendations"])


def test_empty_db_recommendations(tmp_path):
    db_path = tmp_path / "signals.db"

    result = generate_trader_signal_recommendations(db_path=db_path, limit=5)

    assert result["recommendation_count"] == 0
    assert result["recommendations"] == []
