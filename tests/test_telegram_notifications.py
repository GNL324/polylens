from __future__ import annotations

import json

from src.integrations.telegram_notifications import (
    format_daily_intelligence_report,
    generate_daily_intelligence_report,
    is_valid_wallet_address,
    paper_pnl_report_text,
    paper_positions_report_text,
    portfolio_report_text,
    history_report_text,
    equity_report_text,
    paper_recent_report_text,
    paper_strategies_report_text,
    polymarket_analytics_wallet_url,
    strategy_stats_report_text,
    trade_report_text,
    TelegramNotificationConfig,
    TelegramNotificationService,
    wallet_stats_report_text,
)


VALID_WALLET = "0x7af3f727e86394ca3986a1f786b888c7904e83fe"
WALLET_URL = f"https://polymarketanalytics.com/traders/{VALID_WALLET}"


def _paper_report():
    return {
        "read_only": True,
        "paper_only": True,
        "daily_pnl": 1.25,
        "pnl_7d": 12.84,
        "total_pnl": 103.55,
        "open_positions_count": 2,
        "closed_positions_count": 46,
        "win_rate": 0.583,
        "trade_count": 48,
        "recent_trades": [
            {
                "strategy": "early_entry",
                "status": "WIN",
                "pnl": 4.2,
                "realized_pnl": 4.2,
                "unrealized_pnl": 0,
                "market_title": "Will BTC close above 100k?",
                "opened_at": "2026-06-24T10:15:00Z",
                "closed_at": "2026-06-24T13:45:00Z",
            },
            {
                "strategy": "conviction",
                "status": "OPEN",
                "pnl": 0,
                "realized_pnl": 0,
                "unrealized_pnl": 0,
                "market_title": "Will ETH close green?",
                "opened_at": "2026-06-24T14:00:00Z",
                "closed_at": None,
            },
        ],
        "open_positions": [
            {
                "strategy": "conviction",
                "status": "OPEN",
                "unrealized_pnl": 0.5,
                "market_title": "Will ETH close green?",
                "opened_at": "2026-06-24T14:00:00Z",
            }
        ],
        "strategy_breakdown": {
            "early_entry": {"strategy": "early_entry", "trade_count": 10, "realized_pnl": 44.2, "unrealized_pnl": 0, "win_rate": 0.6},
            "conviction": {"strategy": "conviction", "trade_count": 12, "realized_pnl": -2.0, "unrealized_pnl": 0, "win_rate": 0.5},
            "BTC 5m momentum": {"strategy": "BTC 5m momentum", "trade_count": 3, "realized_pnl": 1.0, "unrealized_pnl": 0, "win_rate": 0.667},
        },
        "top_strategy": {"strategy": "early_entry", "realized_pnl": 44.2, "unrealized_pnl": 0, "trade_count": 10, "win_rate": 0.6},
        "worst_strategy": {"strategy": "conviction", "realized_pnl": -2.0, "unrealized_pnl": 0, "trade_count": 12, "win_rate": 0.5},
    }


def _portfolio_report():
    return {
        "portfolio": {
            "cash": 99.0,
            "invested_capital": 2.0,
            "total_equity": 101.0,
            "available_buying_power": 99.0,
            "open_positions": 1,
            "closed_positions": 1,
        },
        "pnl": {"today": 1.0, "seven_day": 1.0, "thirty_day": 1.0, "all_time": 1.0},
        "ledger": [
            {
                "event_type": "CLOSE",
                "action": "SELL",
                "paper_position_id": 1,
                "realized_pnl": 1.0,
                "strategy": "early_entry",
                "market": "Will BTC close above 100k?",
            }
        ],
        "equity_curve": [{"timestamp": "2026-06-24T13:45:00Z", "equity": 101.0, "drawdown": 0.0}],
    }


def test_daily_report_includes_enhanced_paper_section(monkeypatch, tmp_path):
    class FakeDiscovery:
        def __init__(self, **kwargs):
            pass

        def load_recent_discoveries(self, *, limit):
            return []

    class FakePerformance:
        def __init__(self, **kwargs):
            pass

        def load_actions(self, *, action, limit):
            return []

    monkeypatch.setattr("src.integrations.telegram_notifications.WalletDiscoveryEngine", FakeDiscovery)
    monkeypatch.setattr("src.integrations.telegram_notifications.WalletPerformanceEngine", FakePerformance)
    monkeypatch.setattr("src.integrations.telegram_notifications.trader_signal_report", lambda **kwargs: {"summary": {}, "by_signal_type": [], "performance": []})
    monkeypatch.setattr("src.integrations.telegram_notifications.trader_signal_health", lambda **kwargs: {"status": "healthy"})
    monkeypatch.setattr("src.integrations.telegram_notifications.wallet_service_health_summary", lambda **kwargs: {"status": "healthy", "stale_cycles": []})
    monkeypatch.setattr("src.integrations.telegram_notifications.paper_trading_intelligence", lambda **kwargs: _paper_report())

    report = generate_daily_intelligence_report(
        traders_db_path=tmp_path / "traders.db",
        discovery_db_path=tmp_path / "discovery.db",
        signal_db_path=tmp_path / "signals.db",
        paper_db_path=tmp_path / "paper.db",
    )
    text = format_daily_intelligence_report(report)

    assert "Paper Trading" in text
    assert "Daily PnL: +$1.25" in text
    assert "7D PnL: +$12.84" in text
    assert "30D PnL: $0.00" in text
    assert "Total PnL: +$103.55" in text
    assert "Portfolio: +$100.00" in text
    assert "Cash: +$100.00" in text
    assert "Equity: +$100.00" in text
    assert "Buying Power: +$100.00" in text
    assert "Current drawdown: $0.00" in text
    assert "Current exposure: 0.0%" in text
    assert "Open positions: 2" in text
    assert "Closed positions: 46" in text
    assert "Recent: early_entry WIN +$4.20" in text
    assert "Top strategy: early_entry +$44.20 (10 trades)" in text
    assert "Worst strategy: conviction -$2.00 (12 trades)" in text


def test_paper_report_formatters_are_concise():
    payload = _paper_report()

    assert "Recent Paper Trades" in paper_recent_report_text(payload)
    assert "Paper PnL" in paper_pnl_report_text(payload)
    assert "Open Paper Positions" in paper_positions_report_text(payload)
    strategies = paper_strategies_report_text(payload)

    assert "BTC 5m momentum: +$1.00 trades=3 win=66.7%" in strategies
    assert all(len(text) < 3500 for text in (paper_recent_report_text(payload), paper_pnl_report_text(payload), paper_positions_report_text(payload), strategies))


def test_portfolio_report_formatters_are_concise():
    portfolio = _portfolio_report()
    trade = {
        "paper_position_id": 1,
        "net_pnl": 1.0,
        "duration_seconds": 3600,
        "wallet": VALID_WALLET,
        "strategy": "early_entry",
        "market": "Will BTC close above 100k?",
        "exit_reason": "simulated_exit",
    }
    wallet = {
        "wallet": VALID_WALLET,
        "trade_count": 1,
        "realized_pnl": 1.0,
        "unrealized_pnl": 0.0,
        "roi": 0.01,
        "win_rate": 1.0,
        "expectancy": 1.0,
        "sharpe_score": 0,
        "average_holding_period_seconds": 3600,
    }
    strategy = [{"strategy": "early_entry", "total_pnl": 1.0, "trade_count": 1, "win_rate": 1.0, "daily_pnl": 1.0, "weekly_pnl": 1.0}]
    texts = [
        portfolio_report_text(portfolio),
        history_report_text(portfolio),
        equity_report_text(portfolio),
        trade_report_text(trade),
        wallet_stats_report_text(wallet),
        strategy_stats_report_text(strategy),
    ]

    assert "Paper Portfolio" in texts[0]
    assert "Paper History" in texts[1]
    assert "Paper Equity Curve" in texts[2]
    assert "Paper Trade #1" in texts[3]
    assert "Wallet Stats" in texts[4]
    assert "Strategy Stats" in texts[5]
    assert all(len(text) < 3500 for text in texts)


def test_wallet_url_creation_validates_evm_addresses():
    assert is_valid_wallet_address(VALID_WALLET) is True
    assert polymarket_analytics_wallet_url(VALID_WALLET) == WALLET_URL
    assert is_valid_wallet_address("wallet-123") is False
    assert polymarket_analytics_wallet_url("wallet-123") is None
    assert polymarket_analytics_wallet_url("0x123") is None


def test_wallet_promotion_notification_adds_polymarket_button(tmp_path):
    calls = []
    config = TelegramNotificationConfig(bot_token="token", chat_id="999", audit_db_path=str(tmp_path / "telegram.db"))
    service = TelegramNotificationService(config, request_sender=lambda method, params, token: calls.append(params) or {"ok": True})

    service.send_wallet_promotion({"wallet": VALID_WALLET, "performance_summary": "score 91", "promotion_reason": "consistent alpha"})

    markup = json.loads(calls[0]["reply_markup"])
    urls = [button["url"] for row in markup["inline_keyboard"] for button in row if "url" in button]
    assert WALLET_URL in urls


def test_wallet_discovery_notification_rejects_malformed_wallet_links(tmp_path):
    calls = []
    config = TelegramNotificationConfig(bot_token="token", chat_id="999", audit_db_path=str(tmp_path / "telegram.db"))
    service = TelegramNotificationService(config, request_sender=lambda method, params, token: calls.append(params) or {"ok": True})

    service.send_wallet_discovery({"wallets": [VALID_WALLET, "wallet-123"], "summary_metrics": {"count": 2}})

    markup = json.loads(calls[0]["reply_markup"])
    urls = [button["url"] for row in markup["inline_keyboard"] for button in row if "url" in button]
    assert urls == [WALLET_URL]
