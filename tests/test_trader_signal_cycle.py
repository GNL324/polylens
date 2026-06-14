from __future__ import annotations

import json
import sys

from src.analysis.trader_signal_engine import run_trader_signal_cycle, trader_signal_health
from src.sqlite_utils import closing_connection

WALLET = "0x" + "a" * 40


def _activity_export(events):
    return {
        "wallet": WALLET,
        "export_timestamp": "2026-06-14T00:00:00Z",
        "source": "test",
        "event_count": len(events),
        "events": events,
    }


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


def test_run_trader_signal_cycle_with_activity_and_outcomes(tmp_path):
    db_path = tmp_path / "signals.db"
    activity_path = tmp_path / "activity.json"
    outcomes_path = tmp_path / "outcomes.json"
    activity_path.write_text(json.dumps(_activity_export([_event("buy", tx_hash="0xbuy1")])), encoding="utf-8")

    result = run_trader_signal_cycle(
        activity_path=activity_path,
        outcomes_path=None,
        db_path=db_path,
        recommendation_limit=5,
    )

    assert result["read_only"] is True
    assert result["paper_only"] is True
    assert result["signals_generated"] >= 1
    assert result["recommendations"]["recommendation_count"] >= 1
    with closing_connection(db_path) as conn:
        cycle_count = conn.execute("SELECT COUNT(*) FROM trader_signal_cycles").fetchone()[0]
    assert cycle_count == 1

    signals = run_trader_signal_cycle(activity_path=activity_path, db_path=db_path)["persisted"]
    assert signals["signals_inserted"] == 0
    assert signals["signals_skipped"] >= 1

    outcomes_path.write_text(
        json.dumps(
            {
                "outcomes": [
                    {
                        "outcome_key": "outcome-1",
                        "signal_key": result["recommendations"]["recommendations"][0]["recommendation_key"].split(":")[0],
                        "wallet": WALLET,
                        "market_id": "market-1",
                        "signal_type": "early_entry",
                        "entry_price": 0.5,
                        "exit_price": 0.75,
                        "pnl": 0.25,
                        "roi": 0.5,
                        "outcome": "win",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    outcome_cycle = run_trader_signal_cycle(
        activity_path=activity_path,
        outcomes_path=outcomes_path,
        db_path=db_path,
    )
    assert outcome_cycle["performance"]["performance_inserted"] >= 0


def test_cycle_safe_without_optional_paths(tmp_path):
    db_path = tmp_path / "signals.db"

    result = run_trader_signal_cycle(db_path=db_path)

    assert result["signals_generated"] == 0
    assert result["recommendations"]["recommendation_count"] == 0
    health = trader_signal_health(db_path=db_path)
    assert health["last_cycle"] is not None


def test_trader_signal_cycle_cli_json_output(capsys, monkeypatch, tmp_path):
    from src.cli import main

    db_path = tmp_path / "signals.db"
    expected = {"read_only": True, "paper_only": True, "signals_generated": 0}

    def fake_cycle(**kwargs):
        return expected

    monkeypatch.setattr("src.cli.run_trader_signal_cycle", fake_cycle)
    sys.argv = ["polylens", "trader-signal-cycle", "--db-path", str(db_path), "--json"]
    main()
    output = json.loads(capsys.readouterr().out)
    assert output == expected
