from __future__ import annotations

import inspect
import json

from src.analysis import trader_signal_engine
from src.analysis.trader_signal_engine import (
    generate_and_persist_signals_from_activity_path,
    generate_signals_from_activity,
    init_trader_signal_db,
    persist_signals,
    trader_signal_health,
)
from src.sqlite_utils import closing_connection

WALLET = "0x" + "a" * 40
WALLET_B = "0x" + "b" * 40


def _event(action: str, **overrides):
    row = {
        "wallet": WALLET,
        "timestamp": 100,
        "event_type": action,
        "market_id": "market-1",
        "condition_id": "condition-1",
        "market_slug": "bitcoin-up-or-down",
        "market_title": "Bitcoin Up or Down",
        "asset": "BTC",
        "side": "up",
        "action": action,
        "shares": 10,
        "price": 0.5,
        "amount": 5.0,
        "tx_hash": f"0x{action}",
        "raw": {"type": action},
    }
    row.update(overrides)
    return row


def _activity_export(events, wallet: str = WALLET):
    return {
        "wallet": wallet,
        "export_timestamp": "2026-06-14T00:00:00Z",
        "source": "test",
        "event_count": len(events),
        "events": events,
    }


def test_generate_signals_from_wallet_activity(tmp_path):
    events = [
        _event("buy", tx_hash="0xbuy1", amount=250.0),
        _event("sell", timestamp=200, tx_hash="0xsell1", price=0.7),
    ]

    signals = generate_signals_from_activity(events)

    signal_types = {signal["signal_type"] for signal in signals}
    assert "early_entry" in signal_types
    assert "conviction" in signal_types
    assert "exit" in signal_types
    assert all(signal["wallet"] == WALLET for signal in signals)


def test_persist_signals_is_idempotent(tmp_path):
    db_path = tmp_path / "signals.db"
    signals = generate_signals_from_activity([_event("buy", tx_hash="0xbuy1", amount=250.0)])

    first = persist_signals(signals, db_path=db_path)
    second = persist_signals(signals, db_path=db_path)

    assert first["signals_inserted"] == len(signals)
    assert second["signals_inserted"] == 0
    assert second["signals_skipped"] == len(signals)
    with closing_connection(db_path) as conn:
        count = conn.execute("SELECT COUNT(*) FROM trader_signals").fetchone()[0]
    assert count == len(signals)


def test_generate_and_persist_from_activity_path(tmp_path):
    db_path = tmp_path / "signals.db"
    activity_path = tmp_path / "activity.json"
    activity_path.write_text(json.dumps(_activity_export([_event("buy", tx_hash="0xbuy1")])), encoding="utf-8")

    result = generate_and_persist_signals_from_activity_path(activity_path, db_path=db_path)

    assert result["read_only"] is True
    assert result["paper_only"] is True
    assert result["events_loaded"] == 1
    assert result["signals_generated"] >= 1
    assert result["signals_inserted"] >= 1


def test_consensus_signal_for_multi_wallet_market():
    events = [
        _event("buy", wallet=WALLET, tx_hash="0xbuy-a"),
        _event("buy", wallet=WALLET_B, tx_hash="0xbuy-b"),
    ]

    signals = generate_signals_from_activity(events)

    assert any(signal["signal_type"] == "consensus" for signal in signals)


def test_empty_db_health_is_safe(tmp_path):
    db_path = tmp_path / "signals.db"

    health = trader_signal_health(db_path=db_path)

    assert health["read_only"] is True
    assert health["paper_only"] is True
    assert health["status"] == "empty"
    assert health["signal_count"] == 0


def test_json_shape_stability_for_generation(tmp_path):
    db_path = tmp_path / "signals.db"
    init_trader_signal_db(db_path)
    result = persist_signals(generate_signals_from_activity([_event("buy", tx_hash="0xbuy1")]), db_path=db_path)

    assert set(result.keys()) >= {"read_only", "paper_only", "signals_received", "signals_inserted", "signals_skipped"}


def test_trader_signal_module_has_no_live_execution_imports_or_calls():
    source = inspect.getsource(trader_signal_engine)
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
