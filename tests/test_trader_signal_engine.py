from __future__ import annotations

import inspect
import json
from pathlib import Path

from src.analysis import trader_signal_engine
from src.analysis.trader_signal_engine import (
    _normalize_event,
    _parse_timestamp,
    generate_and_persist_signals_from_activity_path,
    generate_signals_from_activity,
    init_trader_signal_db,
    load_activity_json,
    persist_signals,
    run_trader_signal_cycle,
    trader_signal_health,
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


def test_loader_accepts_events(tmp_path):
    activity_path = tmp_path / "events.json"
    activity_path.write_text(json.dumps(_activity_export([_event("buy", tx_hash="0xbuy1")])), encoding="utf-8")

    events = load_activity_json(activity_path)

    assert len(events) == 1
    assert events[0]["action"] == "buy"


def test_loader_accepts_activities(tmp_path):
    activity_path = tmp_path / "activities.json"
    activity_path.write_text(
        json.dumps(
            {
                "wallet_address": WALLET,
                "activities": [
                    {
                        "conditionId": "0xbtc001",
                        "title": "Bitcoin Up or Down",
                        "outcome": "Up",
                        "side": "BUY",
                        "type": "TRADE",
                        "price": 0.5,
                        "size": 20,
                        "usdcSize": 10,
                        "timestamp": 1780140852,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    events = load_activity_json(activity_path)

    assert len(events) == 1
    assert events[0]["wallet"] == WALLET
    assert events[0]["action"] == "buy"
    assert events[0]["market_id"] == "0xbtc001"


def test_loader_parses_iso_timestamp():
    event = _normalize_event(
        {
            "wallet": WALLET,
            "market_id": "market-1",
            "action": "buy",
            "side": "up",
            "price": 0.5,
            "amount": 5,
            "timestamp": "2026-06-14T18:00:00Z",
        }
    )

    assert event["timestamp"] == _parse_timestamp("2026-06-14T18:00:00Z")
    assert event["timestamp"] > 0


def test_simple_smoke_buy_generates_at_least_one_signal():
    event = _normalize_event(
        {
            "wallet": WALLET,
            "market_id": "market-smoke",
            "side": "up",
            "price": 0.4,
            "size": 10,
            "amount": 4,
            "timestamp": "2026-06-14T18:00:00Z",
            "action": "buy",
        }
    )

    signals = generate_signals_from_activity([event])

    assert len(signals) >= 1
    assert any(signal["signal_type"] == "early_entry" for signal in signals)


def test_wallet_forensics_fixture_generates_nonzero_signals():
    events = load_activity_json(FIXTURES / "wallet_forensics" / "market_maker_wallet.json")

    signals = generate_signals_from_activity(events)

    assert len(events) > 0
    assert len(signals) > 0
    assert any(signal["signal_type"] == "early_entry" for signal in signals)
    assert any(signal["signal_type"] == "exit" for signal in signals)


def test_cycle_persists_nonzero_signals_from_smoke_file(tmp_path):
    db_path = tmp_path / "signals.db"
    activity_path = FIXTURES / "trader_signal_smoke_activity.json"

    result = run_trader_signal_cycle(activity_path=activity_path, db_path=db_path)

    assert result["read_only"] is True
    assert result["paper_only"] is True
    assert result["signals_generated"] > 0
    assert result["persisted"]["signals_inserted"] > 0
    with closing_connection(db_path) as conn:
        count = conn.execute("SELECT COUNT(*) FROM trader_signals").fetchone()[0]
    assert count > 0


def test_health_reflects_nonzero_signal_count_after_cycle(tmp_path):
    db_path = tmp_path / "signals.db"
    run_trader_signal_cycle(
        activity_path=FIXTURES / "trader_signal_smoke_activity.json",
        db_path=db_path,
    )

    health = trader_signal_health(db_path=db_path)

    assert health["read_only"] is True
    assert health["paper_only"] is True
    assert health["signal_count"] > 0
    assert health["status"] != "empty"

