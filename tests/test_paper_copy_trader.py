from __future__ import annotations

import inspect
import json
import sys
from types import SimpleNamespace

from src.analysis import paper_copy_trader
from src.analysis.paper_copy_trader import (
    load_watched_wallets,
    paper_copy_report,
    run_paper_copy_trader,
    watch_trader,
)
from src.analysis.wallet_activity import WalletActivityEvent
from src.sqlite_utils import closing_connection

WALLET = "0x" + "a" * 40


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
    return WalletActivityEvent(**row)


class FakeExporter:
    def __init__(self, events):
        self.events = events
        self.calls = []

    def __call__(self, wallet, limit=None, source=None, store=False):
        self.calls.append({"wallet": wallet, "limit": limit, "store": store})
        return SimpleNamespace(wallet=wallet, events=list(self.events))


def test_adding_watched_trader(tmp_path):
    db_path = tmp_path / "paper_copy.db"

    result = watch_trader(WALLET.upper(), db_path=db_path, alpha_score=88)

    assert result["wallet"] == WALLET
    assert load_watched_wallets(db_path) == [WALLET]
    with closing_connection(db_path) as conn:
        row = conn.execute("SELECT alpha_score, status FROM watched_traders WHERE wallet=?", (WALLET,)).fetchone()
    assert row["alpha_score"] == 88
    assert row["status"] == "active"


def test_detecting_new_events_and_creating_paper_positions(tmp_path):
    db_path = tmp_path / "paper_copy.db"
    watch_trader(WALLET, db_path=db_path)
    exporter = FakeExporter([_event("buy", tx_hash="0xbuy1", price=0.5)])

    result = run_paper_copy_trader(db_path=db_path, exporter=exporter)

    assert result["events_seen"] == 1
    assert result["new_events"] == 1
    assert result["copied_trades"] == 1
    assert result["open_positions"] == 1
    with closing_connection(db_path) as conn:
        row = conn.execute("SELECT entry_price, shares, notional, status FROM paper_copy_positions").fetchone()
    assert row["entry_price"] == 0.5
    assert row["shares"] == 2
    assert row["notional"] == 1
    assert row["status"] == "open"


def test_deduping_already_copied_events(tmp_path):
    db_path = tmp_path / "paper_copy.db"
    watch_trader(WALLET, db_path=db_path)
    exporter = FakeExporter([_event("buy", tx_hash="0xbuy1")])

    first = run_paper_copy_trader(db_path=db_path, exporter=exporter)
    second = run_paper_copy_trader(db_path=db_path, exporter=exporter)

    assert first["copied_trades"] == 1
    assert second["new_events"] == 0
    assert second["copied_trades"] == 0
    assert paper_copy_report(db_path)["copied_trades"] == 1


def test_closing_paper_positions(tmp_path):
    db_path = tmp_path / "paper_copy.db"
    watch_trader(WALLET, db_path=db_path)
    run_paper_copy_trader(db_path=db_path, exporter=FakeExporter([_event("buy", tx_hash="0xbuy1", price=0.5)]))

    result = run_paper_copy_trader(
        db_path=db_path,
        exporter=FakeExporter([
            _event("buy", tx_hash="0xbuy1", price=0.5),
            _event("sell", timestamp=200, tx_hash="0xsell1", price=0.75, amount=7.5),
        ]),
    )

    report = paper_copy_report(db_path)
    assert result["closed_positions"] == 1
    assert report["open_positions"] == 0
    assert report["closed_positions"] == 1
    assert report["realized_pnl"] == 0.5
    assert report["roi"] == 0.5


def test_report_metrics_by_wallet_and_asset(tmp_path):
    db_path = tmp_path / "paper_copy.db"
    watch_trader(WALLET, db_path=db_path)
    run_paper_copy_trader(db_path=db_path, exporter=FakeExporter([_event("buy", tx_hash="0xbuy1", price=0.5)]))
    run_paper_copy_trader(db_path=db_path, exporter=FakeExporter([_event("sell", timestamp=200, tx_hash="0xsell1", price=0.25)]))

    report = paper_copy_report(db_path)

    assert report["copied_trades"] == 1
    assert report["closed_positions"] == 1
    assert report["realized_pnl"] == -0.5
    assert report["win_rate"] == 0
    assert report["by_wallet"][WALLET]["realized_pnl"] == -0.5
    assert report["by_asset"]["BTC"]["closed_positions"] == 1


def test_report_by_wallet_matches_full_report(tmp_path):
    """paper_copy_report_by_wallet must produce the same per-wallet stats as paper_copy_report."""
    from src.analysis.paper_copy_trader import paper_copy_report_by_wallet

    db_path = tmp_path / "paper_copy.db"
    watch_trader(WALLET, db_path=db_path)
    run_paper_copy_trader(db_path=db_path, exporter=FakeExporter([_event("buy", tx_hash="0xbuy1", price=0.5)]))
    run_paper_copy_trader(db_path=db_path, exporter=FakeExporter([_event("sell", timestamp=200, tx_hash="0xsell1", price=0.25)]))

    full = paper_copy_report(db_path)
    by_wallet = paper_copy_report_by_wallet(WALLET, db_path=db_path)
    full_wallet = full["by_wallet"][WALLET]

    assert by_wallet["copied_trades"] == full_wallet["copied_trades"]
    assert by_wallet["open_positions"] == full_wallet["open_positions"]
    assert by_wallet["closed_positions"] == full_wallet["closed_positions"]
    assert by_wallet["realized_pnl"] == full_wallet["realized_pnl"]
    assert by_wallet["roi"] == full_wallet["roi"]


def test_report_by_wallet_for_nonexistent_wallet_returns_zeros(tmp_path):
    """paper_copy_report_by_wallet for a wallet with no positions should return zeros."""
    from src.analysis.paper_copy_trader import paper_copy_report_by_wallet

    db_path = tmp_path / "paper_copy.db"
    watch_trader(WALLET, db_path=db_path)

    result = paper_copy_report_by_wallet("0x" + "z" * 40, db_path=db_path)
    assert result["copied_trades"] == 0
    assert result["open_positions"] == 0
    assert result["closed_positions"] == 0
    assert result["realized_pnl"] == 0.0
    assert result["roi"] == 0.0
    assert result["win_rate"] == 0.0


def test_report_segment_single_pass_notional(tmp_path):
    """_report_segment must compute notional in a single pass, not O(N²) scan."""
    from src.analysis.paper_copy_trader import _report_segment

    rows = [
        {"source_wallet": "0xaaa", "status": "closed", "pnl": 1.0, "notional": 10.0},
        {"source_wallet": "0xaaa", "status": "open", "pnl": 0.0, "notional": 5.0},
        {"source_wallet": "0xbbb", "status": "closed", "pnl": -2.0, "notional": 20.0},
        {"source_wallet": "0xbbb", "status": "closed", "pnl": 3.0, "notional": 15.0},
    ]
    result = _report_segment(rows, "source_wallet")
    assert result["0xaaa"]["realized_pnl"] == 1.0
    assert result["0xaaa"]["closed_positions"] == 1
    assert result["0xaaa"]["roi"] == round(1.0 / 10.0, 6)
    assert result["0xbbb"]["realized_pnl"] == 1.0
    assert result["0xbbb"]["closed_positions"] == 2
    assert result["0xbbb"]["roi"] == round(1.0 / 35.0, 6)


def test_run_respects_max_stake_per_wallet_per_run(tmp_path):
    db_path = tmp_path / "paper_copy.db"
    watch_trader(WALLET, db_path=db_path)
    events = [_event("buy", tx_hash=f"0xbuy{i}", timestamp=100 + i, market_id=f"m{i}") for i in range(12)]

    result = run_paper_copy_trader(db_path=db_path, exporter=FakeExporter(events), stake=1, max_stake_per_wallet=10)

    assert result["new_events"] == 12
    assert result["copied_trades"] == 10
    assert paper_copy_report(db_path)["open_positions"] == 10


def test_paper_copy_trader_cli_json_output(capsys, monkeypatch, tmp_path):
    from src.cli import main

    db_path = tmp_path / "paper_copy.db"
    expected = {"copied_trades": 1, "open_positions": 1}

    def fake_report(db_path):
        return expected

    monkeypatch.setattr("src.cli.paper_copy_report", fake_report)
    sys.argv = ["polylens", "paper-copy-trader", "--report", "--db-path", str(db_path), "--json"]
    main()
    output = json.loads(capsys.readouterr().out)
    assert output == expected


def test_paper_copy_module_has_no_live_execution_imports_or_calls():
    source = inspect.getsource(paper_copy_trader)
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


def test_start_validation_window_snapshots_baseline_and_is_idempotent(tmp_path):
    from src.analysis.paper_copy_trader import load_validation_window, start_validation_window

    db_path = tmp_path / "paper_copy.db"
    watch_trader(WALLET, db_path=db_path)

    first = start_validation_window(db_path=db_path)
    assert first["started"] is True
    assert first["baseline_wallets"] == [WALLET]

    other_wallet = "0x" + "b" * 40
    watch_trader(other_wallet, db_path=db_path)
    second = start_validation_window(db_path=db_path)
    assert second["started"] is False
    assert second["window_start"] == first["window_start"]

    window = load_validation_window(db_path=db_path)
    assert window["baseline_wallets"] == [WALLET]


def test_pre_validation_report_distinguishes_no_signals_from_zero_pnl(tmp_path):
    from src.analysis.paper_copy_trader import pre_validation_report

    db_path = tmp_path / "paper_copy.db"
    signal_db_path = tmp_path / "trader_signals.db"
    traders_db_path = tmp_path / "traders.db"

    no_signal_wallet = "0x" + "c" * 40
    watch_trader(no_signal_wallet, db_path=db_path)

    zero_pnl_wallet = WALLET
    watch_trader(zero_pnl_wallet, db_path=db_path)
    run_paper_copy_trader(db_path=db_path, exporter=FakeExporter([_event("buy", tx_hash="0xbuy1", price=0.5)]))
    run_paper_copy_trader(
        db_path=db_path,
        exporter=FakeExporter([_event("sell", timestamp=200, tx_hash="0xsell1", price=0.5)]),
    )

    report = pre_validation_report(db_path=db_path, signal_db_path=signal_db_path, traders_db_path=traders_db_path)
    by_wallet = {row["wallet"]: row for row in report["wallets"]}

    assert by_wallet[no_signal_wallet]["data_status"] == "no_signals_yet"
    assert by_wallet[no_signal_wallet]["signal_count"] == 0
    assert by_wallet[no_signal_wallet]["closed_positions"] == 0

    assert by_wallet[zero_pnl_wallet]["data_status"] == "zero_pnl_active"
    assert by_wallet[zero_pnl_wallet]["closed_positions"] == 1
    assert by_wallet[zero_pnl_wallet]["realized_pnl"] == 0.0
    assert report["data_status_counts"]["no_signals_yet"] == 1
    assert report["data_status_counts"]["zero_pnl_active"] == 1


def test_pre_validation_report_tags_cohort_from_validation_window(tmp_path):
    from src.analysis.paper_copy_trader import pre_validation_report, start_validation_window

    db_path = tmp_path / "paper_copy.db"
    baseline_wallet = WALLET
    watch_trader(baseline_wallet, db_path=db_path)
    start_validation_window(db_path=db_path)

    late_wallet = "0x" + "d" * 40
    watch_trader(late_wallet, db_path=db_path)

    report = pre_validation_report(db_path=db_path, signal_db_path=tmp_path / "signals.db", traders_db_path=tmp_path / "traders.db")
    by_wallet = {row["wallet"]: row for row in report["wallets"]}

    assert by_wallet[baseline_wallet]["cohort"] == "baseline"
    assert by_wallet[late_wallet]["cohort"] == "joined_during_window"
    assert by_wallet[late_wallet]["cohort_start"] == by_wallet[late_wallet]["watched_at"]


def _seed_signal(signal_db_path, wallet, *, signal_key):
    from src.analysis.trader_signal_engine import init_trader_signal_db

    init_trader_signal_db(signal_db_path)
    with closing_connection(signal_db_path) as conn:
        conn.execute(
            """
            INSERT INTO trader_signals (
                signal_key, wallet, market_id, market_title, asset, side,
                signal_type, timestamp, action, price, shares, amount,
                score, tx_hash, raw_json, created_at
            ) VALUES (?, ?, 'm1', 'Test Market', 'BTC', 'up', 'early_entry', 100, 'buy', 0.5, 10, 5.0, 50.0, ?, '{}', '2026-01-01T00:00:00Z')
            """,
            (signal_key, wallet, f"0x{signal_key}"),
        )


def test_pre_validation_report_covers_signals_no_trades_open_only_and_active(tmp_path):
    from src.analysis.paper_copy_trader import pre_validation_report

    db_path = tmp_path / "paper_copy.db"
    signal_db_path = tmp_path / "trader_signals.db"
    traders_db_path = tmp_path / "traders.db"

    signals_no_trades_wallet = "0x" + "1" * 40
    watch_trader(signals_no_trades_wallet, db_path=db_path)
    _seed_signal(signal_db_path, signals_no_trades_wallet, signal_key="s1")

    open_only_wallet = "0x" + "2" * 40
    watch_trader(open_only_wallet, db_path=db_path)
    run_paper_copy_trader(
        db_path=db_path,
        wallets=[open_only_wallet],
        exporter=FakeExporter([_event("buy", wallet=open_only_wallet, tx_hash="0xbuy_open", price=0.5)]),
    )

    active_wallet = "0x" + "3" * 40
    watch_trader(active_wallet, db_path=db_path)
    run_paper_copy_trader(
        db_path=db_path,
        wallets=[active_wallet],
        exporter=FakeExporter([_event("buy", wallet=active_wallet, tx_hash="0xbuy_active", price=0.5)]),
    )
    run_paper_copy_trader(
        db_path=db_path,
        wallets=[active_wallet],
        exporter=FakeExporter([_event("sell", wallet=active_wallet, timestamp=200, tx_hash="0xsell_active", price=0.75)]),
    )

    report = pre_validation_report(db_path=db_path, signal_db_path=signal_db_path, traders_db_path=traders_db_path)
    by_wallet = {row["wallet"]: row for row in report["wallets"]}

    assert by_wallet[signals_no_trades_wallet]["data_status"] == "signals_no_trades"
    assert by_wallet[signals_no_trades_wallet]["signal_count"] == 1
    assert by_wallet[signals_no_trades_wallet]["copied_trades"] == 0

    assert by_wallet[open_only_wallet]["data_status"] == "open_only"
    assert by_wallet[open_only_wallet]["open_positions"] == 1
    assert by_wallet[open_only_wallet]["closed_positions"] == 0

    assert by_wallet[active_wallet]["data_status"] == "active"
    assert by_wallet[active_wallet]["closed_positions"] == 1
    assert by_wallet[active_wallet]["realized_pnl"] == 0.5
