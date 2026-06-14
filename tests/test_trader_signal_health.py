from __future__ import annotations

import json
import sys

from src.analysis.trader_signal_engine import (
    generate_signals_from_activity,
    persist_signals,
    run_trader_signal_cycle,
    trader_signal_health,
    trader_signal_report,
)

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


def test_health_after_cycle(tmp_path):
    db_path = tmp_path / "signals.db"
    persist_signals(generate_signals_from_activity([_event("buy", tx_hash="0xbuy1")]), db_path=db_path)
    run_trader_signal_cycle(db_path=db_path)

    health = trader_signal_health(db_path=db_path)

    assert health["read_only"] is True
    assert health["paper_only"] is True
    assert health["status"] in {"healthy", "idle"}
    assert health["signal_count"] >= 1
    assert health["last_cycle"] is not None
    assert set(health.keys()) >= {
        "status",
        "db_path",
        "signal_count",
        "scored_count",
        "recommendation_count",
        "performance_count",
        "last_cycle",
        "read_only",
        "paper_only",
    }


def test_report_json_shape(tmp_path):
    db_path = tmp_path / "signals.db"
    persist_signals(generate_signals_from_activity([_event("buy", tx_hash="0xbuy1")]), db_path=db_path)

    report = trader_signal_report(db_path=db_path, include_recommendations=True, recommendation_limit=5)

    assert report["read_only"] is True
    assert report["paper_only"] is True
    assert "summary" in report
    assert "by_signal_type" in report
    assert "performance" in report
    assert "recommendations" in report


def test_trader_signal_health_cli_json_output(capsys, monkeypatch, tmp_path):
    from src.cli import main

    db_path = tmp_path / "signals.db"
    expected = {"read_only": True, "paper_only": True, "status": "empty"}

    def fake_health(**kwargs):
        return expected

    monkeypatch.setattr("src.cli.trader_signal_health", fake_health)
    sys.argv = ["polylens", "trader-signal-health", "--db-path", str(db_path), "--json"]
    main()
    output = json.loads(capsys.readouterr().out)
    assert output == expected
