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
    start_validation_window,
    unwatch_trader,
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


def test_unwatch_trader_removes_wallet_from_watchlist_but_keeps_history(tmp_path):
    db_path = tmp_path / "paper_copy.db"
    watch_trader(WALLET, db_path=db_path)
    exporter = FakeExporter([_event("buy", tx_hash="0xbuy1", price=0.5)])
    run_paper_copy_trader(db_path=db_path, exporter=exporter)

    result = unwatch_trader(WALLET, db_path=db_path)

    assert result["unwatched"] is True
    assert load_watched_wallets(db_path) == []
    with closing_connection(db_path) as conn:
        row = conn.execute("SELECT status FROM watched_traders WHERE wallet=?", (WALLET,)).fetchone()
        positions = conn.execute("SELECT COUNT(*) AS n FROM paper_copy_positions WHERE source_wallet=?", (WALLET,)).fetchone()
    assert row["status"] == "excluded"
    assert positions["n"] == 1


def test_unwatch_trader_unknown_wallet_reports_not_found(tmp_path):
    db_path = tmp_path / "paper_copy.db"

    result = unwatch_trader(WALLET, db_path=db_path)

    assert result["unwatched"] is False


def test_start_validation_window_force_overwrites_after_unwatch(tmp_path):
    db_path = tmp_path / "paper_copy.db"
    other_wallet = "0x" + "b" * 40
    watch_trader(WALLET, db_path=db_path)
    watch_trader(other_wallet, db_path=db_path)
    first = start_validation_window(db_path=db_path)
    assert first["baseline_wallets"] == sorted([WALLET, other_wallet])

    unwatch_trader(WALLET, db_path=db_path)
    unchanged = start_validation_window(db_path=db_path)
    assert unchanged["started"] is False

    refreshed = start_validation_window(db_path=db_path, force=True)

    assert refreshed["started"] is True
    assert refreshed["baseline_wallets"] == [other_wallet]


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


def test_redeem_with_zero_payout_closes_position_as_a_loss(tmp_path):
    """A redeem with no payout means the market resolved against this position.

    It must close at $0, not stay open forever (which would silently bias
    win rate and realized P&L toward wins only).
    """
    db_path = tmp_path / "paper_copy.db"
    watch_trader(WALLET, db_path=db_path)
    run_paper_copy_trader(db_path=db_path, exporter=FakeExporter([_event("buy", tx_hash="0xbuy1", price=0.5)]))

    result = run_paper_copy_trader(
        db_path=db_path,
        exporter=FakeExporter([
            _event("buy", tx_hash="0xbuy1", price=0.5),
            _event("redeem", timestamp=200, tx_hash="0xredeem1", price=0, amount=0, shares=0),
        ]),
    )

    report = paper_copy_report(db_path)
    assert result["closed_positions"] == 1
    assert report["open_positions"] == 0
    assert report["closed_positions"] == 1
    assert report["realized_pnl"] == -1.0
    assert report["roi"] == -1.0
    assert report["win_rate"] == 0.0


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


def test_paper_copy_trader_cli_unwatch_flag(capsys, monkeypatch, tmp_path):
    from src.cli import main

    db_path = tmp_path / "paper_copy.db"
    calls = []

    def fake_unwatch(wallet, db_path):
        calls.append((wallet, db_path))
        return {"wallet": wallet, "unwatched": True, "status": "excluded"}

    monkeypatch.setattr("src.cli.unwatch_paper_copy_trader", fake_unwatch)
    sys.argv = ["polylens", "paper-copy-trader", "--unwatch", WALLET, "--db-path", str(db_path), "--json"]
    main()
    output = json.loads(capsys.readouterr().out)
    assert output == {"wallet": WALLET, "unwatched": True, "status": "excluded"}
    assert calls == [(WALLET, str(db_path))]


def test_paper_copy_trader_cli_start_validation_window_force_flag(capsys, monkeypatch, tmp_path):
    from src.cli import main

    db_path = tmp_path / "paper_copy.db"
    calls = []

    def fake_start_window(db_path, force):
        calls.append((db_path, force))
        return {"started": True, "force": force}

    monkeypatch.setattr("src.cli.start_validation_window", fake_start_window)
    sys.argv = [
        "polylens",
        "paper-copy-trader",
        "--start-validation-window",
        "--force",
        "--db-path",
        str(db_path),
        "--json",
    ]
    main()
    output = json.loads(capsys.readouterr().out)
    assert output == {"started": True, "force": True}
    assert calls == [(str(db_path), True)]


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


def _insert_stalled_redeem_event(db_path, *, event_key: str, wallet: str = WALLET) -> None:
    """Simulate a zero-payout redeem recorded under the pre-fix _exit_price bug.

    Before the fix this event was fetched and saved (copied=0) but never closed
    the position it belonged to, because _exit_price returned None for it.
    """
    with closing_connection(db_path) as conn:
        conn.execute(
            """
            INSERT INTO paper_copy_events (
                run_id, event_key, source_wallet, timestamp, action, market_id,
                market_title, asset, side, price, shares, amount, tx_hash, copied, raw_json
            ) VALUES (1, ?, ?, 200, 'redeem', 'market-1', 'Bitcoin Up or Down', 'BTC', 'up', 0, 0, 0, '0xredeem1', 0, '{}')
            """,
            (event_key, wallet),
        )


def test_backfill_closes_positions_stalled_by_the_exit_price_bug(tmp_path):
    from src.analysis.paper_copy_trader import backfill_stalled_zero_payout_exits

    db_path = tmp_path / "paper_copy.db"
    watch_trader(WALLET, db_path=db_path)
    run_paper_copy_trader(db_path=db_path, exporter=FakeExporter([_event("buy", tx_hash="0xbuy1", price=0.5)]))
    _insert_stalled_redeem_event(db_path, event_key="stale-event-key")

    result = backfill_stalled_zero_payout_exits(db_path=db_path)

    report = paper_copy_report(db_path)
    assert result["candidates_scanned"] == 1
    assert result["events_replayed"] == 1
    assert result["positions_closed"] == 1
    assert report["open_positions"] == 0
    assert report["closed_positions"] == 1
    assert report["realized_pnl"] == -1.0
    assert report["win_rate"] == 0.0


def test_backfill_is_idempotent_and_skips_already_settled_events(tmp_path):
    from src.analysis.paper_copy_trader import backfill_stalled_zero_payout_exits

    db_path = tmp_path / "paper_copy.db"
    watch_trader(WALLET, db_path=db_path)
    run_paper_copy_trader(db_path=db_path, exporter=FakeExporter([_event("buy", tx_hash="0xbuy1", price=0.5)]))
    _insert_stalled_redeem_event(db_path, event_key="stale-event-key")

    first = backfill_stalled_zero_payout_exits(db_path=db_path)
    second = backfill_stalled_zero_payout_exits(db_path=db_path)

    assert first["positions_closed"] == 1
    assert second["candidates_scanned"] == 1
    assert second["events_replayed"] == 0
    assert second["positions_closed"] == 0
    assert paper_copy_report(db_path)["closed_positions"] == 1


def test_backfill_does_not_touch_already_open_positions_without_a_stalled_redeem(tmp_path):
    from src.analysis.paper_copy_trader import backfill_stalled_zero_payout_exits

    db_path = tmp_path / "paper_copy.db"
    watch_trader(WALLET, db_path=db_path)
    run_paper_copy_trader(db_path=db_path, exporter=FakeExporter([_event("buy", tx_hash="0xbuy1", price=0.5)]))

    result = backfill_stalled_zero_payout_exits(db_path=db_path)

    assert result["candidates_scanned"] == 0
    assert result["positions_closed"] == 0
    assert paper_copy_report(db_path)["open_positions"] == 1


def test_ingestion_gap_report_flags_wallet_whose_reachable_history_is_newer_than_its_open_position(tmp_path):
    from src.analysis.paper_copy_trader import ingestion_gap_report

    db_path = tmp_path / "paper_copy.db"
    watch_trader(WALLET, db_path=db_path)
    run_paper_copy_trader(db_path=db_path, exporter=FakeExporter([_event("buy", tx_hash="0xbuy1", price=0.5, timestamp=100)]))

    # The live wallet fetch can now only reach back to timestamp=200, well
    # after the position at timestamp=100 was opened -- any exit event for it
    # is out of reach, exactly like the wallet found trading too fast for the
    # Polymarket activity-history offset cap to keep up with.
    gap_exporter = FakeExporter([_event("buy", tx_hash="0xbuy2", price=0.5, timestamp=200)])

    result = ingestion_gap_report(db_path=db_path, exporter=gap_exporter)

    assert result["wallets_checked"] == 1
    assert result["wallets_at_risk"] == 1
    row = result["wallets"][0]
    assert row["wallet"] == WALLET
    assert row["open_positions"] == 1
    assert row["oldest_open_position_entry"] == 100
    assert row["oldest_reachable_activity"] == 200
    assert row["ingestion_gap_risk"] is True


def test_ingestion_gap_report_clears_wallet_whose_reachable_history_covers_its_open_position(tmp_path):
    from src.analysis.paper_copy_trader import ingestion_gap_report

    db_path = tmp_path / "paper_copy.db"
    watch_trader(WALLET, db_path=db_path)
    run_paper_copy_trader(db_path=db_path, exporter=FakeExporter([_event("buy", tx_hash="0xbuy1", price=0.5, timestamp=100)]))

    # This time the fetch reaches all the way back past the position's entry.
    covering_exporter = FakeExporter([_event("buy", tx_hash="0xbuy2", price=0.5, timestamp=50)])

    result = ingestion_gap_report(db_path=db_path, exporter=covering_exporter)

    assert result["wallets_checked"] == 1
    assert result["wallets_at_risk"] == 0
    assert result["wallets"][0]["ingestion_gap_risk"] is False


def test_ingestion_gap_report_skips_wallets_with_no_open_positions(tmp_path):
    from src.analysis.paper_copy_trader import ingestion_gap_report

    db_path = tmp_path / "paper_copy.db"
    watch_trader(WALLET, db_path=db_path)

    result = ingestion_gap_report(db_path=db_path, exporter=FakeExporter([]))

    assert result["wallets_checked"] == 0
    assert result["wallets_at_risk"] == 0
    assert result["wallets"] == []


def test_paper_copy_trader_cli_ingestion_gap_report_flag(capsys, monkeypatch, tmp_path):
    from src.cli import main

    db_path = tmp_path / "paper_copy.db"
    calls = []

    def fake_ingestion_gap_report(db_path):
        calls.append(db_path)
        return {"wallets_checked": 1, "wallets_at_risk": 1}

    monkeypatch.setattr("src.cli.ingestion_gap_report", fake_ingestion_gap_report)
    sys.argv = ["polylens", "paper-copy-trader", "--ingestion-gap-report", "--db-path", str(db_path), "--json"]
    main()
    output = json.loads(capsys.readouterr().out)
    assert output == {"wallets_checked": 1, "wallets_at_risk": 1}
    assert calls == [str(db_path)]


def test_run_records_watermark_and_detects_no_gap_on_first_run(tmp_path):
    from src.analysis.paper_copy_trader import coverage_gap_report

    db_path = tmp_path / "paper_copy.db"
    watch_trader(WALLET, db_path=db_path)

    result = run_paper_copy_trader(
        db_path=db_path,
        exporter=FakeExporter([_event("buy", tx_hash="0xbuy1", price=0.5, timestamp=100)]),
    )

    assert result["coverage_gaps_detected"] == 0
    with closing_connection(db_path) as conn:
        row = conn.execute(
            "SELECT newest_event_timestamp, oldest_event_timestamp FROM wallet_ingestion_watermarks WHERE wallet=?",
            (WALLET,),
        ).fetchone()
    assert row["newest_event_timestamp"] == 100
    assert row["oldest_event_timestamp"] == 100
    assert coverage_gap_report(db_path)["total_gaps"] == 0


def test_run_detects_coverage_gap_when_next_fetch_does_not_reach_back_to_prior_watermark(tmp_path):
    from src.analysis.paper_copy_trader import coverage_gap_report

    db_path = tmp_path / "paper_copy.db"
    watch_trader(WALLET, db_path=db_path)
    run_paper_copy_trader(
        db_path=db_path,
        exporter=FakeExporter([_event("buy", tx_hash="0xbuy1", price=0.5, timestamp=100)]),
    )

    # The next scheduled run's fetch only reaches back to timestamp=500 --
    # there's a real, never-observed span between 100 and 500 where a
    # sell/redeem could have happened and would now be permanently missed.
    result = run_paper_copy_trader(
        db_path=db_path,
        exporter=FakeExporter([_event("buy", tx_hash="0xbuy2", price=0.5, timestamp=500)]),
    )

    assert result["coverage_gaps_detected"] == 1
    report = coverage_gap_report(db_path)
    assert report["total_gaps"] == 1
    assert report["wallets_affected"] == [WALLET]
    gap = report["gaps"][0]
    assert gap["gap_start"] == 100
    assert gap["gap_end"] == 500
    assert gap["gap_seconds"] == 400


def test_run_does_not_flag_a_gap_when_the_next_fetch_still_overlaps_the_prior_watermark(tmp_path):
    from src.analysis.paper_copy_trader import coverage_gap_report

    db_path = tmp_path / "paper_copy.db"
    watch_trader(WALLET, db_path=db_path)
    run_paper_copy_trader(
        db_path=db_path,
        exporter=FakeExporter([_event("buy", tx_hash="0xbuy1", price=0.5, timestamp=100)]),
    )

    # A real production fetch returns the full reachable history each time,
    # so the next run's oldest event should still reach back to (or past)
    # the previous newest -- continuous coverage, no gap.
    result = run_paper_copy_trader(
        db_path=db_path,
        exporter=FakeExporter(
            [
                _event("buy", tx_hash="0xbuy1", price=0.5, timestamp=100),
                _event("sell", tx_hash="0xsell1", price=0.6, timestamp=150),
            ]
        ),
    )

    assert result["coverage_gaps_detected"] == 0
    assert coverage_gap_report(db_path)["total_gaps"] == 0


def test_run_with_explicit_limit_does_not_track_watermark(tmp_path):
    """A truncated fetch (--limit) isn't a trustworthy signal of real coverage depth."""
    from src.analysis.paper_copy_trader import coverage_gap_report

    db_path = tmp_path / "paper_copy.db"
    watch_trader(WALLET, db_path=db_path)
    run_paper_copy_trader(
        db_path=db_path,
        limit=1,
        exporter=FakeExporter([_event("buy", tx_hash="0xbuy1", price=0.5, timestamp=100)]),
    )
    run_paper_copy_trader(
        db_path=db_path,
        limit=1,
        exporter=FakeExporter([_event("buy", tx_hash="0xbuy2", price=0.5, timestamp=999)]),
    )

    with closing_connection(db_path) as conn:
        row = conn.execute("SELECT 1 FROM wallet_ingestion_watermarks WHERE wallet=?", (WALLET,)).fetchone()
    assert row is None
    assert coverage_gap_report(db_path)["total_gaps"] == 0


def test_paper_copy_trader_cli_coverage_gap_report_flag(capsys, monkeypatch, tmp_path):
    from src.cli import main

    db_path = tmp_path / "paper_copy.db"
    calls = []

    def fake_coverage_gap_report(db_path):
        calls.append(db_path)
        return {"total_gaps": 2, "wallets_affected": [WALLET]}

    monkeypatch.setattr("src.cli.coverage_gap_report", fake_coverage_gap_report)
    sys.argv = ["polylens", "paper-copy-trader", "--coverage-gap-report", "--db-path", str(db_path), "--json"]
    main()
    output = json.loads(capsys.readouterr().out)
    assert output == {"total_gaps": 2, "wallets_affected": [WALLET]}
    assert calls == [str(db_path)]


def test_unwatch_ingestion_gap_risks_excludes_only_flagged_wallets(tmp_path):
    from src.analysis.paper_copy_trader import unwatch_ingestion_gap_risks

    db_path = tmp_path / "paper_copy.db"
    wallet_2 = "0x" + "b" * 40
    watch_trader(WALLET, db_path=db_path)
    watch_trader(wallet_2, db_path=db_path)
    run_paper_copy_trader(
        db_path=db_path,
        wallets=[WALLET],
        exporter=FakeExporter([_event("buy", tx_hash="0xbuy1", price=0.5, timestamp=100)]),
    )
    run_paper_copy_trader(
        db_path=db_path,
        wallets=[wallet_2],
        exporter=FakeExporter([_event("buy", wallet=wallet_2, tx_hash="0xbuy2", price=0.5, timestamp=100)]),
    )

    class PerWalletExporter:
        def __call__(self, wallet, limit=None, source=None, store=False):
            # WALLET's live fetch can no longer reach back to its position at
            # timestamp=100 -- flagged. wallet_2's fetch still covers it fine.
            oldest = 200 if wallet == WALLET else 50
            return SimpleNamespace(wallet=wallet, events=[_event("buy", tx_hash="0xlive", price=0.5, timestamp=oldest)])

    result = unwatch_ingestion_gap_risks(db_path=db_path, exporter=PerWalletExporter())

    assert result["wallets_checked"] == 2
    assert result["count_excluded"] == 1
    assert result["wallets_excluded"] == [WALLET]
    assert load_watched_wallets(db_path) == [wallet_2]


def test_paper_copy_trader_cli_unwatch_ingestion_gap_risks_flag(capsys, monkeypatch, tmp_path):
    from src.cli import main

    db_path = tmp_path / "paper_copy.db"
    calls = []

    def fake_unwatch_ingestion_gap_risks(db_path):
        calls.append(db_path)
        return {"wallets_checked": 2, "wallets_excluded": [WALLET], "count_excluded": 1}

    monkeypatch.setattr("src.cli.unwatch_ingestion_gap_risks", fake_unwatch_ingestion_gap_risks)
    sys.argv = ["polylens", "paper-copy-trader", "--unwatch-ingestion-gap-risks", "--db-path", str(db_path), "--json"]
    main()
    output = json.loads(capsys.readouterr().out)
    assert output == {"wallets_checked": 2, "wallets_excluded": [WALLET], "count_excluded": 1}
    assert calls == [str(db_path)]
