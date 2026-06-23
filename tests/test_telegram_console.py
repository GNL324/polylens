from __future__ import annotations

import sqlite3

import pytest

from src.integrations.telegram_console import (
    TelegramConsole,
    TelegramConsoleConfig,
    TelegramConsoleConfigError,
    TelegramResponse,
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
