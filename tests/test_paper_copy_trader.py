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
