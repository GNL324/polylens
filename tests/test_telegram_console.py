from __future__ import annotations

import sqlite3

import pytest

from src.integrations.telegram_console import (
    TelegramConsole,
    TelegramConsoleConfig,
    TelegramConsoleConfigError,
    TelegramResponse,
)
from src.integrations.telegram_notifications import (
    TelegramNotificationConfig,
    TelegramNotificationService,
    format_daily_intelligence_report,
    generate_daily_intelligence_report,
)


def _config(tmp_path, admins=(123,), token="secret-token") -> TelegramConsoleConfig:
    return TelegramConsoleConfig(
        bot_token=token,
        admin_user_ids=frozenset(admins),
        audit_db_path=str(tmp_path / "telegram.db"),
    )


def _callback_ids(reply_markup):
    return {
        button["callback_data"]
        for row in reply_markup["inline_keyboard"]
        for button in row
    }


def test_unauthorized_user_rejected(tmp_path):
    console = TelegramConsole(_config(tmp_path, admins=(123,)))

    assert console.handle_text(999, "/status") == "unauthorized"


def test_admin_user_allowed(tmp_path):
    console = TelegramConsole(_config(tmp_path, admins=(123,)))

    response = console.handle_text(123, "/status")

    assert "read-only" in response
    assert "paper-only=true" in response


def test_token_redacted(tmp_path):
    config = _config(tmp_path, token="123456:super-secret")

    safe = config.safe_dict()

    assert safe["bot_token"] == "redacted"
    assert "super-secret" not in str(safe)


def test_missing_admin_allowlist_fails_closed(tmp_path):
    console = TelegramConsole(_config(tmp_path, admins=()))

    assert console.handle_text(123, "/status") == "admin allowlist missing"
    with pytest.raises(TelegramConsoleConfigError):
        console.validate_startup()


def test_live_command_blocked(tmp_path):
    console = TelegramConsole(_config(tmp_path, admins=(123,)))

    assert console.handle_text(123, "/kill_switch") == "live trading disabled"


def test_help_returns_command_list(tmp_path):
    console = TelegramConsole(_config(tmp_path, admins=(123,)))

    response = console.handle_text(123, "/help")

    assert "/status" in response
    assert "/wallet <address>" in response
    assert "/kill_switch" in response


def test_help_returns_main_menu_reply_markup(tmp_path):
    console = TelegramConsole(_config(tmp_path, admins=(123,)))

    response = console.handle_text(123, "/help")

    assert response.reply_markup is not None
    assert _callback_ids(response.reply_markup) == {
        "menu_intelligence",
        "menu_wallets",
        "menu_signals",
        "menu_system",
        "menu_reports",
    }


def test_start_returns_main_menu_reply_markup(tmp_path):
    console = TelegramConsole(_config(tmp_path, admins=(123,)))

    response = console.handle_text(123, "/start")

    assert response.reply_markup is not None
    assert "menu_system" in _callback_ids(response.reply_markup)


def test_menu_navigation_to_system_menu(tmp_path):
    console = TelegramConsole(_config(tmp_path, admins=(123,)))

    response = console.handle_callback(123, "menu_system")

    assert response == "System\nSafe service, paper, and risk status."
    assert response.reply_markup is not None
    assert _callback_ids(response.reply_markup) == {
        "status",
        "health",
        "paper_status",
        "risk",
        "menu_main",
    }


def test_back_navigation_returns_main_menu(tmp_path):
    console = TelegramConsole(_config(tmp_path, admins=(123,)))

    response = console.handle_callback(123, "menu_main")

    assert response == "Polylens Control Console\nChoose a category."
    assert response.reply_markup is not None
    assert _callback_ids(response.reply_markup) == {
        "menu_intelligence",
        "menu_wallets",
        "menu_signals",
        "menu_system",
        "menu_reports",
    }


def test_health_uses_safe_health_path(tmp_path):
    called = {"health": False}

    def health_provider():
        called["health"] = True
        return {"status": "healthy", "success_rate": 1.0, "stale_cycles": [], "failures": []}

    console = TelegramConsole(_config(tmp_path, admins=(123,)), health_provider=health_provider)

    response = console.handle_text(123, "/health")

    assert called["health"] is True
    assert response == "Health: healthy; success_rate=1.00; stale=0; failures=0"


def test_callback_health_routes_to_health_logic(tmp_path):
    called = {"health": False}

    def health_provider():
        called["health"] = True
        return {"status": "healthy", "success_rate": 1.0, "stale_cycles": [], "failures": []}

    console = TelegramConsole(_config(tmp_path, admins=(123,)), health_provider=health_provider)

    response = console.handle_callback(123, "health")

    assert called["health"] is True
    assert response == "Health: healthy; success_rate=1.00; stale=0; failures=0"
    assert response.reply_markup is not None


def test_report_callback_daily_brief(monkeypatch, tmp_path):
    console = TelegramConsole(_config(tmp_path, admins=(123,)))
    report = {
        "generated_at": "2026-06-23T12:00:00Z",
        "wallet_intelligence": {"new_discoveries": [], "promotions": [], "demotions": [], "retirements": []},
        "signals": {"summary": {"total_signals": 3}, "by_signal_type": [], "performance": []},
        "paper_trading": {"daily_pnl": 1.5, "open_positions": 2, "closed_positions": 4, "win_rate": 0.5},
        "system_health": {"wallet_autonomy_status": "healthy", "signal_engine_status": "healthy", "critical_warnings": []},
    }
    monkeypatch.setattr("src.integrations.telegram_console.generate_daily_intelligence_report", lambda: report)

    response = console.handle_callback(123, "report_daily")

    assert "Polylens Daily Intelligence Brief" in response
    assert "Daily PnL: $1.50" in response
    assert response.reply_markup is not None
    assert "report_paper" in _callback_ids(response.reply_markup)


def test_unauthorized_callback_rejected(tmp_path):
    console = TelegramConsole(_config(tmp_path, admins=(123,)))

    response = console.handle_callback(999, "health")

    assert response == "unauthorized"
    assert response.reply_markup is None


def test_unknown_callback_handled_safely(tmp_path):
    console = TelegramConsole(_config(tmp_path, admins=(123,)))

    response = console.handle_callback(123, "does_not_exist")

    assert response == "unknown action. Try /help"
    assert response.reply_markup is not None


def test_callback_audit_row_written(tmp_path):
    config = _config(tmp_path, admins=(123,))
    console = TelegramConsole(config)

    console.handle_callback(123, "health")

    with sqlite3.connect(config.audit_db_path) as conn:
        row = conn.execute(
            """
            SELECT telegram_user_id, command, args, allowed, result_status, error_message
            FROM telegram_command_audit
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()

    assert row == (123, "callback:health", "", 1, "ok", None)


def test_menu_callback_audit_row_written(tmp_path):
    config = _config(tmp_path, admins=(123,))
    console = TelegramConsole(config)

    console.handle_callback(123, "menu_system")

    with sqlite3.connect(config.audit_db_path) as conn:
        row = conn.execute(
            """
            SELECT telegram_user_id, command, args, allowed, result_status, error_message
            FROM telegram_command_audit
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()

    assert row == (123, "callback:menu_system", "", 1, "ok", None)


def test_live_like_callback_blocked(tmp_path):
    console = TelegramConsole(_config(tmp_path, admins=(123,)))

    response = console.handle_callback(123, "kill_switch")

    assert response == "live trading disabled"
    assert response.reply_markup is None


def test_message_edit_used_when_possible(tmp_path):
    console = TelegramConsole(_config(tmp_path, admins=(123,)))
    calls = []

    def fake_request(method, params):
        calls.append((method, params))
        return {"ok": True}

    console._telegram_request = fake_request

    console._edit_message_or_send(456, 789, TelegramResponse("Updated", {"inline_keyboard": []}))

    assert [method for method, _params in calls] == ["editMessageText"]
    assert calls[0][1]["chat_id"] == 456
    assert calls[0][1]["message_id"] == 789
    assert calls[0][1]["reply_markup"] == '{"inline_keyboard": []}'


def test_message_edit_falls_back_to_send(tmp_path):
    console = TelegramConsole(_config(tmp_path, admins=(123,)))
    calls = []

    def fake_request(method, params):
        calls.append((method, params))
        if method == "editMessageText":
            raise RuntimeError("edit failed")
        return {"ok": True}

    console._telegram_request = fake_request

    console._edit_message_or_send(456, 789, TelegramResponse("Updated"))

    assert [method for method, _params in calls] == ["editMessageText", "sendMessage"]
    assert calls[1][1] == {"chat_id": 456, "text": "Updated"}


def test_audit_row_written(tmp_path):
    config = _config(tmp_path, admins=(123,))
    console = TelegramConsole(config)

    console.handle_text(123, "/help")

    with sqlite3.connect(config.audit_db_path) as conn:
        row = conn.execute(
            """
            SELECT telegram_user_id, command, args, allowed, result_status, error_message
            FROM telegram_command_audit
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()

    assert row == (123, "/help", "", 1, "ok", None)


def test_signal_notification_sent_with_buttons_and_audit(tmp_path):
    calls = []

    def fake_sender(method, params, token):
        calls.append((method, params, token))
        return {"ok": True}

    config = TelegramNotificationConfig(
        bot_token="123456:secret",
        chat_id="999",
        audit_db_path=str(tmp_path / "telegram.db"),
    )
    service = TelegramNotificationService(config, request_sender=fake_sender)

    result = service.send_high_conviction_signal(
        {
            "strategy_name": "wallet-alpha",
            "confidence": 0.88,
            "market_title": "BTC above 100k",
            "signal_timestamp": "2026-06-23T12:00:00Z",
        }
    )

    assert result["sent"] is True
    assert calls[0][0] == "sendMessage"
    assert "High-conviction signal" in calls[0][1]["text"]
    assert "123456:secret" not in calls[0][1]["text"]
    assert "reply_markup" in calls[0][1]
    with sqlite3.connect(config.audit_db_path) as conn:
        row = conn.execute(
            """
            SELECT command, notification_sent, notification_type, delivery_status
            FROM telegram_command_audit
            ORDER BY id DESC LIMIT 1
            """
        ).fetchone()
    assert row == ("notification:high_conviction_signal", 1, "high_conviction_signal", "sent")


def test_wallet_promotion_notification(tmp_path):
    calls = []
    config = TelegramNotificationConfig(
        bot_token="token",
        chat_id="999",
        audit_db_path=str(tmp_path / "telegram.db"),
    )
    service = TelegramNotificationService(config, request_sender=lambda method, params, token: calls.append(params) or {"ok": True})

    result = service.send_wallet_promotion(
        {
            "wallet": "0x" + "a" * 40,
            "performance_summary": "score 91 win rate 64%",
            "promotion_reason": "consistent alpha",
        }
    )

    assert result["delivery_status"] == "sent"
    assert "Wallet promotion" in calls[0]["text"]
    assert "consistent alpha" in calls[0]["text"]


def test_autonomy_alert_notification(tmp_path):
    calls = []
    config = TelegramNotificationConfig(
        bot_token="token",
        chat_id="999",
        audit_db_path=str(tmp_path / "telegram.db"),
    )
    service = TelegramNotificationService(config, request_sender=lambda method, params, token: calls.append(params) or {"ok": True})

    result = service.send_wallet_autonomy_failure(
        {"failed_cycle": "signals", "error_summary": "timeout", "timestamp": "2026-06-23T12:00:00Z"}
    )

    assert result["sent"] is True
    assert "Wallet autonomy failure" in calls[0]["text"]
    assert "signals" in calls[0]["text"]


def test_disabled_notification_configuration(tmp_path):
    calls = []
    config = TelegramNotificationConfig(
        bot_token="token",
        chat_id="999",
        notifications_enabled=False,
        audit_db_path=str(tmp_path / "telegram.db"),
    )
    service = TelegramNotificationService(config, request_sender=lambda method, params, token: calls.append(params) or {"ok": True})

    result = service.send_system_health_alert({"service": "wallet-autonomy", "status": "unhealthy"})

    assert result == {
        "sent": False,
        "delivery_status": "disabled",
        "notification_type": "system_health_alert",
    }
    assert calls == []


def test_daily_report_generation(monkeypatch, tmp_path):
    class FakeDiscovery:
        def __init__(self, **kwargs):
            pass

        def load_recent_discoveries(self, *, limit):
            return [{"wallet": "0xabc"}]

    class FakePerformance:
        def __init__(self, **kwargs):
            pass

        def load_actions(self, *, action, limit):
            return [{"wallet": f"0x{action}", "action": action}]

    monkeypatch.setattr("src.integrations.telegram_notifications.WalletDiscoveryEngine", FakeDiscovery)
    monkeypatch.setattr("src.integrations.telegram_notifications.WalletPerformanceEngine", FakePerformance)
    monkeypatch.setattr(
        "src.integrations.telegram_notifications.trader_signal_report",
        lambda **kwargs: {
            "summary": {"total_signals": 5, "wallets": 2, "markets": 3},
            "by_signal_type": [{"signal_type": "conviction", "count": 2}],
            "performance": [{"outcome": "proven", "count": 1}],
        },
    )
    monkeypatch.setattr(
        "src.integrations.telegram_notifications.trader_signal_health",
        lambda **kwargs: {"status": "healthy", "signal_count": 5},
    )
    monkeypatch.setattr(
        "src.integrations.telegram_notifications.performance_report",
        lambda **kwargs: {"realized_pnl": 3.25, "open_positions": 1, "closed_positions": 4, "win_rate": 0.75, "roi": 0.1},
    )
    monkeypatch.setattr(
        "src.integrations.telegram_notifications.wallet_service_health_summary",
        lambda **kwargs: {"status": "healthy", "stale_cycles": []},
    )

    report = generate_daily_intelligence_report(
        traders_db_path=tmp_path / "traders.db",
        discovery_db_path=tmp_path / "discovery.db",
        signal_db_path=tmp_path / "signals.db",
        paper_db_path=tmp_path / "paper.db",
    )
    text = format_daily_intelligence_report(report)

    assert report["read_only"] is True
    assert report["paper_only"] is True
    assert report["wallet_intelligence"]["new_discoveries"] == [{"wallet": "0xabc"}]
    assert "Polylens Daily Intelligence Brief" in text
    assert "Daily PnL: $3.25" in text
