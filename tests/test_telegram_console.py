from __future__ import annotations

import sqlite3

import pytest

from src.integrations.telegram_console import (
    TelegramConsole,
    TelegramConsoleConfig,
    TelegramConsoleConfigError,
)


def _config(tmp_path, admins=(123,), token="secret-token") -> TelegramConsoleConfig:
    return TelegramConsoleConfig(
        bot_token=token,
        admin_user_ids=frozenset(admins),
        audit_db_path=str(tmp_path / "telegram.db"),
    )


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


def test_health_uses_safe_health_path(tmp_path):
    called = {"health": False}

    def health_provider():
        called["health"] = True
        return {"status": "healthy", "success_rate": 1.0, "stale_cycles": [], "failures": []}

    console = TelegramConsole(_config(tmp_path, admins=(123,)), health_provider=health_provider)

    response = console.handle_text(123, "/health")

    assert called["health"] is True
    assert response == "Health: healthy; success_rate=1.00; stale=0; failures=0"


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
