from __future__ import annotations

import json

from src.analysis.trader_signal_engine import (
    generate_signals_from_activity,
    persist_signals,
    score_signal_row,
    score_trader_signals,
    update_signal_performance,
)
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
        "amount": 5.0,
        "tx_hash": f"0x{action}",
    }
    row.update(overrides)
    return row


def test_score_signal_row_bounds():
    low_price = score_signal_row({"signal_type": "early_entry", "price": 0.2, "amount": 5})
    high_conviction = score_signal_row({"signal_type": "conviction", "price": 0.5, "amount": 500})

    assert 0 <= low_price <= 100
    assert 0 <= high_conviction <= 100
    assert high_conviction >= low_price


def test_score_trader_signals_updates_null_scores(tmp_path):
    db_path = tmp_path / "signals.db"
    signals = generate_signals_from_activity([_event("buy", tx_hash="0xbuy1", amount=250.0)])
    persist_signals(signals, db_path=db_path)

    result = score_trader_signals(db_path=db_path)

    assert result["read_only"] is True
    assert result["paper_only"] is True
    assert result["signals_scored"] == len(signals)
    with closing_connection(db_path) as conn:
        null_scores = conn.execute("SELECT COUNT(*) FROM trader_signals WHERE score IS NULL").fetchone()[0]
    assert null_scores == 0


def test_score_trader_signals_is_idempotent_on_rescore(tmp_path):
    db_path = tmp_path / "signals.db"
    persist_signals(generate_signals_from_activity([_event("buy", tx_hash="0xbuy1")]), db_path=db_path)

    first = score_trader_signals(db_path=db_path)
    second = score_trader_signals(db_path=db_path)

    assert first["signals_scored"] >= 1
    assert second["signals_scored"] == 0


def test_update_signal_performance_idempotent(tmp_path):
    db_path = tmp_path / "signals.db"
    signals = generate_signals_from_activity([_event("buy", tx_hash="0xbuy1")])
    persist_signals(signals, db_path=db_path)
    score_trader_signals(db_path=db_path)
    signal_key = signals[0]["signal_key"]
    outcome = {
        "outcome_key": "outcome-1",
        "signal_key": signal_key,
        "wallet": WALLET,
        "market_id": "market-1",
        "signal_type": signals[0]["signal_type"],
        "entry_price": 0.5,
        "exit_price": 0.75,
        "pnl": 0.25,
        "roi": 0.5,
        "outcome": "win",
    }

    first = update_signal_performance([outcome], db_path=db_path)
    second = update_signal_performance([outcome], db_path=db_path)

    assert first["performance_inserted"] == 1
    assert second["performance_inserted"] == 0
    assert second["performance_skipped"] == 1


def test_empty_db_scoring_and_performance(tmp_path):
    db_path = tmp_path / "signals.db"

    score_result = score_trader_signals(db_path=db_path)
    perf_result = update_signal_performance([], db_path=db_path)

    assert score_result["signals_scored"] == 0
    assert perf_result["performance_inserted"] == 0
