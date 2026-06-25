from __future__ import annotations

import json

from src.integrations.telegram_notifications import (
    format_daily_intelligence_report,
    generate_daily_intelligence_report,
    is_valid_wallet_address,
    paper_pnl_report_text,
    paper_positions_report_text,
    paper_recent_report_text,
    paper_strategies_report_text,
    polymarket_analytics_wallet_url,
    TelegramNotificationConfig,
    TelegramNotificationService,
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
    assert "Total PnL: +$103.55" in text
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
