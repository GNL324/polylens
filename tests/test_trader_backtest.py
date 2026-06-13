from __future__ import annotations

import json
import sys

from src.analysis.trader_backtest import backtest_wallet_activity

WALLET = "0x" + "a" * 40


def _event(action: str, **overrides):
    row = {
        "wallet": WALLET,
        "timestamp": 100,
        "event_type": action,
        "market_id": "market-1",
        "condition_id": "condition-1",
        "market_slug": "bitcoin-up-or-down-5m",
        "market_title": "Bitcoin Up or Down 5m",
        "asset": "BTC",
        "side": "up",
        "action": action,
        "shares": 10,
        "price": 0.4,
        "amount": 4.0,
        "tx_hash": f"0x{action}",
        "raw": {},
    }
    row.update(overrides)
    return row


def _payload(events):
    return {"wallet": WALLET, "event_count": len(events), "events": events}


def test_simple_buy_redeem_profit():
    report = backtest_wallet_activity(
        _payload([
            _event("buy", timestamp=100, shares=10, price=0.4, amount=4),
            _event("redeem", timestamp=700, shares=10, price=0, amount=10),
        ])
    )

    assert report.closed_trades == 1
    assert report.realized_pnl == 6
    assert report.roi == 1.5
    assert report.win_rate == 1
    assert report.avg_hold_minutes == 10


def test_simple_buy_redeem_loss():
    report = backtest_wallet_activity(
        _payload([
            _event("buy", timestamp=100, shares=10, price=0.8, amount=8),
            _event("redeem", timestamp=700, shares=10, price=0, amount=0.5),
        ])
    )

    assert report.closed_trades == 1
    assert report.realized_pnl == -7.5
    assert report.win_rate == 0
    assert report.largest_loss == -7.5


def test_side_less_redeem_matches_market_inventory():
    report = backtest_wallet_activity(
        _payload([
            _event("buy", timestamp=100, side="up", shares=10, price=0.4, amount=4),
            _event("redeem", timestamp=700, side="", shares=10, price=0, amount=10),
        ])
    )

    assert report.closed_trades == 1
    assert report.open_trades == 0
    assert report.realized_pnl == 6
    assert report.trades[0].side == "up"


def test_side_less_redeem_with_both_sides_stays_unresolved():
    report = backtest_wallet_activity(
        _payload([
            _event("buy", timestamp=100, side="up", shares=10, price=0.4, amount=4),
            _event("buy", timestamp=110, side="down", shares=10, price=0.3, amount=3, tx_hash="0xbuy-down"),
            _event("redeem", timestamp=700, side="", shares=10, price=0, amount=10),
        ])
    )

    assert report.closed_trades == 0
    assert report.open_trades == 2
    assert report.realized_pnl == 0


def test_buy_sell_exit():
    report = backtest_wallet_activity(
        _payload([
            _event("buy", timestamp=100, shares=10, price=0.4, amount=4),
            _event("sell", timestamp=160, shares=10, price=0.55, amount=5.5),
        ])
    )

    assert report.closed_trades == 1
    assert report.realized_pnl == 1.5
    assert report.trades[0].outcome == "sold"


def test_partial_exits_leave_open_inventory():
    report = backtest_wallet_activity(
        _payload([
            _event("buy", timestamp=100, shares=10, price=0.4, amount=4),
            _event("sell", timestamp=160, shares=4, price=0.5, amount=2),
        ])
    )

    assert report.closed_trades == 1
    assert report.open_trades == 1
    assert report.unresolved_count == 1
    assert report.open_notional == 2.4
    assert report.realized_pnl == 0.4


def test_unresolved_trade_handling():
    report = backtest_wallet_activity(_payload([_event("buy", timestamp=100, shares=5, price=0.5, amount=2.5)]))

    assert report.closed_trades == 0
    assert report.open_trades == 1
    assert report.realized_pnl == 0
    assert report.trades[0].pnl is None
    assert report.trades[0].outcome == "open"


def test_merge_count_does_not_create_pnl():
    report = backtest_wallet_activity(
        _payload([
            _event("buy", timestamp=100, shares=10, price=0.4, amount=4),
            _event("merge", timestamp=120, shares=10, price=0, amount=0),
        ])
    )

    assert report.merge_count == 1
    assert report.closed_trades == 0
    assert report.realized_pnl == 0
    assert report.open_trades == 1


def test_both_side_market_handling():
    report = backtest_wallet_activity(
        _payload([
            _event("buy", timestamp=100, side="up", shares=10, price=0.4, amount=4),
            _event("buy", timestamp=110, side="down", shares=10, price=0.3, amount=3, tx_hash="0xbuy-down"),
            _event("sell", timestamp=160, side="up", shares=10, price=0.5, amount=5),
        ])
    )

    assert report.closed_trades == 1
    assert report.open_trades == 1
    assert report.by_side["up"]["pnl"] == 1
    assert report.by_side["down"]["open_trades"] == 1


def test_drawdown_calculation():
    report = backtest_wallet_activity(
        _payload([
            _event("buy", timestamp=100, market_id="m1", amount=4, tx_hash="0xb1"),
            _event("sell", timestamp=110, market_id="m1", amount=6, tx_hash="0xs1"),
            _event("buy", timestamp=120, market_id="m2", amount=5, tx_hash="0xb2"),
            _event("sell", timestamp=130, market_id="m2", amount=1, tx_hash="0xs2"),
        ])
    )

    assert report.realized_pnl == -2
    assert report.max_drawdown == -4
    assert report.largest_win == 2
    assert report.largest_loss == -4


def test_by_asset_segmentation():
    report = backtest_wallet_activity(
        _payload([
            _event("buy", timestamp=100, market_id="btc", asset="BTC", amount=4, tx_hash="0xbb"),
            _event("sell", timestamp=110, market_id="btc", asset="BTC", amount=5, tx_hash="0xsb"),
            _event("buy", timestamp=120, market_id="eth", asset="ETH", amount=3, tx_hash="0xbe"),
            _event("sell", timestamp=130, market_id="eth", asset="ETH", amount=2, tx_hash="0xse"),
        ])
    )

    assert report.by_asset["BTC"]["pnl"] == 1
    assert report.by_asset["ETH"]["pnl"] == -1
    assert report.by_duration["5m"]["trades"] == 2
    assert "directional" in report.by_strategy_profile


def test_trader_backtest_cli_json_output(capsys, monkeypatch):
    from src.analysis.trader_backtest import TraderBacktestReport
    from src.cli import main

    fake_report = TraderBacktestReport(
        wallet=WALLET,
        trade_count=1,
        closed_trades=1,
        win_rate=1,
        total_pnl=1,
        realized_pnl=1,
        roi=0.2,
        avg_trade_return=0.2,
        max_drawdown=0,
        largest_win=1,
        largest_loss=0,
        avg_hold_minutes=5,
        open_trades=0,
        unresolved_count=0,
        open_notional=0,
        merge_count=0,
        redeem_count=1,
        by_asset={"BTC": {"trades": 1}},
        by_side={"up": {"trades": 1}},
        by_duration={"5m": {"trades": 1}},
        by_strategy_profile={"directional": {"trades": 1}},
        trades=[],
    )

    def fake_build(wallet, limit=None):
        assert wallet == WALLET
        assert limit == 25
        return fake_report

    monkeypatch.setattr("src.cli.build_trader_backtest_report", fake_build)
    sys.argv = ["polylens", "trader-backtest", "--wallet", WALLET, "--limit", "25", "--json"]
    main()
    output = json.loads(capsys.readouterr().out)
    assert output["wallet"] == WALLET
    assert output["realized_pnl"] == 1
    assert "trades" not in output
