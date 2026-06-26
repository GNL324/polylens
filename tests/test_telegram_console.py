from __future__ import annotations

import sqlite3
from urllib.error import HTTPError

import pytest

from src.analysis.paper_intelligence import paper_trading_intelligence
from src.analysis.paper_trading_engine import init_paper_trading_db
from src.integrations.telegram_console import (
    MAX_TELEGRAM_TEXT,
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
    polymarket_analytics_wallet_buttons,
)


VALID_WALLET = "0x7af3f727e86394ca3986a1f786b888c7904e83fe"
WALLET_URL = f"https://polymarketanalytics.com/traders/{VALID_WALLET}"


def _config(tmp_path, admins=(123,), token="secret-token") -> TelegramConsoleConfig:
    return TelegramConsoleConfig(
        bot_token=token,
        admin_user_ids=frozenset(admins),
        audit_db_path=str(tmp_path / "telegram.db"),
    )


def _paper_report():
    return {
        "read_only": True,
        "paper_only": True,
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
        "recent_fills": [],
        "open_positions": [
            {
                "strategy": "conviction",
                "status": "OPEN",
                "unrealized_pnl": 0,
                "market_title": "Will ETH close green?",
                "opened_at": "2026-06-24T14:00:00Z",
            }
        ],
        "closed_positions": [],
        "daily_pnl": 0.0,
        "pnl_7d": 12.84,
        "total_pnl": 103.55,
        "win_rate": 0.583,
        "trade_count": 48,
        "open_positions_count": 1,
        "closed_positions_count": 47,
        "strategy_breakdown": {
            "early_entry": {
                "strategy": "early_entry",
                "trade_count": 10,
                "open_positions": 0,
                "closed_positions": 10,
                "realized_pnl": 44.2,
                "unrealized_pnl": 0,
                "win_rate": 0.6,
            },
            "conviction": {
                "strategy": "conviction",
                "trade_count": 12,
                "open_positions": 1,
                "closed_positions": 11,
                "realized_pnl": -2.0,
                "unrealized_pnl": 0,
                "win_rate": 0.5,
            },
        },
        "top_strategy": {"strategy": "early_entry", "realized_pnl": 44.2, "unrealized_pnl": 0, "trade_count": 10, "win_rate": 0.6},
        "worst_strategy": {"strategy": "conviction", "realized_pnl": -2.0, "unrealized_pnl": 0, "trade_count": 12, "win_rate": 0.5},
        "warnings": [],
    }


def _empty_paper_report():
    payload = _paper_report()
    payload.update(
        {
            "recent_trades": [],
            "recent_fills": [],
            "open_positions": [],
            "closed_positions": [],
            "daily_pnl": 0,
            "pnl_7d": 0,
            "total_pnl": 0,
            "win_rate": 0,
            "trade_count": 0,
            "open_positions_count": 0,
            "closed_positions_count": 0,
            "strategy_breakdown": {},
            "top_strategy": None,
            "worst_strategy": None,
        }
    )
    return payload


def _callback_ids(reply_markup):
    return {
        button["callback_data"]
        for row in reply_markup["inline_keyboard"]
        for button in row
    }


def _button_urls(reply_markup):
    return [
        button["url"]
        for row in reply_markup["inline_keyboard"]
        for button in row
        if "url" in button
    ]


def _callback_update(callback_data, *, chat_id=456, user_id=123, message_id=789):
    return {
        "update_id": 100,
        "callback_query": {
            "id": "callback-1",
            "from": {"id": user_id},
            "message": {"message_id": message_id, "chat": {"id": chat_id}},
            "data": callback_data,
        },
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


def test_wallet_command_adds_polymarket_analytics_button(tmp_path):
    console = TelegramConsole(
        _config(tmp_path, admins=(123,)),
        wallet_provider=lambda wallet: {
            "wallet": wallet,
            "classification": "arbitrage_trader",
            "watch_score": 91,
            "confidence": 0.8,
            "report_count": 3,
        },
    )

    response = console.handle_text(123, f"/wallet {VALID_WALLET}")

    assert "Wallet 0x7af3...83fe" in response
    assert response.reply_markup is not None
    assert WALLET_URL in _button_urls(response.reply_markup)


def test_malformed_wallet_command_does_not_create_link(tmp_path):
    console = TelegramConsole(_config(tmp_path, admins=(123,)))

    response = console.handle_text(123, "/wallet not-a-wallet")

    assert response == "wallet: provide a valid 0x address"
    assert response.reply_markup is None


def test_top_wallets_adds_valid_polymarket_analytics_buttons_only(tmp_path):
    console = TelegramConsole(
        _config(tmp_path, admins=(123,)),
        top_wallets_provider=lambda: [
            {"wallet": VALID_WALLET, "watch_score": 91, "classification": "arbitrage"},
            {"wallet": "wallet-123", "watch_score": 12, "classification": "bad"},
        ],
    )

    response = console.handle_text(123, "/top_wallets")

    assert "Top wallets:" in response
    assert response.reply_markup is not None
    assert WALLET_URL in _button_urls(response.reply_markup)
    assert all("wallet-123" not in url for url in _button_urls(response.reply_markup))


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
    assert "Daily PnL: +$1.50" in response
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


def test_reports_menu_contains_paper_intelligence_callbacks(tmp_path):
    console = TelegramConsole(_config(tmp_path, admins=(123,)))

    response = console.handle_callback(123, "menu_reports")

    assert response.reply_markup is not None
    assert _callback_ids(response.reply_markup) == {
        "report_daily",
        "report_signals",
        "report_wallets",
        "report_paper",
        "paper_recent",
        "paper_pnl",
        "paper_positions",
        "paper_strategies",
        "menu_main",
    }


def test_wallet_summary_callback_adds_wallet_link_button(monkeypatch, tmp_path):
    monkeypatch.setattr("src.integrations.telegram_console.wallet_summary_report_text", lambda: f"Wallet Summary\nWallets: {VALID_WALLET}")
    monkeypatch.setattr(
        "src.integrations.telegram_console.wallet_summary_link_buttons",
        lambda: polymarket_analytics_wallet_buttons([VALID_WALLET, "bad-wallet"]),
    )
    console = TelegramConsole(_config(tmp_path, admins=(123,)))

    response = console.handle_callback(123, "report_wallets")

    assert "Wallet Summary" in response
    assert response.reply_markup is not None
    assert WALLET_URL in _button_urls(response.reply_markup)


def test_paper_recent_command(tmp_path):
    payload = _paper_report()
    payload["recent_trades"][0]["wallet"] = VALID_WALLET
    console = TelegramConsole(_config(tmp_path, admins=(123,)), paper_intelligence_provider=lambda: payload)

    response = console.handle_text(123, "/paper_recent")

    assert "Recent Paper Trades" in response
    assert "early_entry | WIN | +$4.20" in response
    assert "Will BTC close above 100k?" in response
    assert f"Wallet: {VALID_WALLET}" in response
    assert response.reply_markup is not None
    assert WALLET_URL in _button_urls(response.reply_markup)


def test_paper_pnl_command(tmp_path):
    console = TelegramConsole(_config(tmp_path, admins=(123,)), paper_intelligence_provider=_paper_report)

    response = console.handle_text(123, "/paper_pnl")

    assert "Paper PnL" in response
    assert "Today: $0.00" in response
    assert "7D: +$12.84" in response
    assert "Total: +$103.55" in response
    assert "Win rate: 58.3%" in response
    assert "Trades: 48" in response


def test_paper_positions_command(tmp_path):
    console = TelegramConsole(_config(tmp_path, admins=(123,)), paper_intelligence_provider=_paper_report)

    response = console.handle_text(123, "/paper_positions")

    assert "Open Paper Positions" in response
    assert "Open: 1" in response
    assert "conviction | $0.00" in response


def test_paper_strategies_command(tmp_path):
    console = TelegramConsole(_config(tmp_path, admins=(123,)), paper_intelligence_provider=_paper_report)

    response = console.handle_text(123, "/paper_strategies")

    assert "Paper Strategies" in response
    assert "early_entry: +$44.20 trades=10 win=60.0%" in response
    assert "conviction: -$2.00 trades=12 win=50.0%" in response


@pytest.mark.parametrize(
    ("callback_id", "heading"),
    [
        ("paper_recent", "Recent Paper Trades"),
        ("paper_pnl", "Paper PnL"),
        ("paper_positions", "Open Paper Positions"),
        ("paper_strategies", "Paper Strategies"),
    ],
)
def test_paper_callbacks_route_and_audit(tmp_path, callback_id, heading):
    config = _config(tmp_path, admins=(123,))
    console = TelegramConsole(config, paper_intelligence_provider=_paper_report)

    response = console.handle_callback(123, callback_id)

    assert heading in response
    assert response.reply_markup is not None
    with sqlite3.connect(config.audit_db_path) as conn:
        row = conn.execute(
            """
            SELECT command, allowed, result_status
            FROM telegram_command_audit
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()
    assert row == (f"callback:{callback_id}", 1, "ok")


@pytest.mark.parametrize("command", ["/paper_recent", "/paper_pnl", "/paper_positions", "/paper_strategies"])
def test_paper_command_audit_rows(tmp_path, command):
    config = _config(tmp_path, admins=(123,))
    console = TelegramConsole(config, paper_intelligence_provider=_paper_report)

    console.handle_text(123, command)

    with sqlite3.connect(config.audit_db_path) as conn:
        row = conn.execute(
            """
            SELECT command, allowed, result_status, error_message
            FROM telegram_command_audit
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()
    assert row == (command, 1, "ok", None)


def test_empty_db_behavior(tmp_path):
    db_path = tmp_path / "empty.db"
    db_path.write_bytes(b"")

    report = paper_trading_intelligence(db_path=db_path, short_crypto_db_path=tmp_path / "missing_short.db")

    assert report["trade_count"] == 0
    assert report["recent_trades"] == []
    assert report["open_positions_count"] == 0


def test_no_paper_trades_behavior(tmp_path):
    console = TelegramConsole(_config(tmp_path, admins=(123,)), paper_intelligence_provider=_empty_paper_report)

    recent = console.handle_text(123, "/paper_recent")
    strategies = console.handle_text(123, "/paper_strategies")

    assert recent == "Recent Paper Trades\nNone"
    assert strategies == "Paper Strategies\nNo paper trades"


def test_paper_outputs_are_telegram_safe(tmp_path):
    payload = _paper_report()
    payload["recent_trades"] = [
        {
            "strategy": "early_entry",
            "status": "WIN",
            "pnl": 1,
            "realized_pnl": 1,
            "unrealized_pnl": 0,
            "market_title": "Very long market title " * 80,
            "opened_at": "2026-06-24T10:15:00Z",
            "closed_at": "2026-06-24T13:45:00Z",
        }
        for _ in range(80)
    ]
    console = TelegramConsole(_config(tmp_path, admins=(123,)), paper_intelligence_provider=lambda: payload)

    response = console.handle_text(123, "/paper_recent")

    assert len(response.text) <= MAX_TELEGRAM_TEXT


def test_paper_intelligence_does_not_mutate_paper_tables(tmp_path):
    db_path = tmp_path / "paper.db"
    init_paper_trading_db(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO paper_orders
            (id, run_id, opportunity_id, strategy, side, market_id, title, asset, simulated_price, stake, status, raw_json)
            VALUES (1, 1, 'opp-1', 'early_entry', 'yes', 'm1', 'Market 1', 'BTC', 0.5, 2, 'filled', '{}')
            """
        )
        conn.execute(
            """
            INSERT INTO paper_positions
            (paper_position_id, order_id, opportunity_id, strategy, market_id, title, asset, side, entry_timestamp, entry_price, shares, notional, status, current_price, exit_timestamp, exit_price, realized_pnl, unrealized_pnl, roi)
            VALUES (1, 1, 'opp-1', 'early_entry', 'm1', 'Market 1', 'BTC', 'yes', '2026-06-24T10:00:00Z', 0.5, 4, 2, 'closed', 0.5, '2026-06-24T11:00:00Z', 0.75, 1, 0, 0.5)
            """
        )
        before = {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("paper_orders", "paper_positions", "paper_settlements", "paper_equity_curve")
        }

    config = _config(tmp_path, admins=(123,))
    config = TelegramConsoleConfig(
        bot_token=config.bot_token,
        admin_user_ids=config.admin_user_ids,
        audit_db_path=config.audit_db_path,
        paper_db_path=str(db_path),
        short_crypto_paper_db_path=str(tmp_path / "missing_short.db"),
    )
    console = TelegramConsole(config)
    console.handle_text(123, "/paper_recent")
    console.handle_text(123, "/paper_pnl")
    console.handle_text(123, "/paper_positions")
    console.handle_text(123, "/paper_strategies")

    with sqlite3.connect(db_path) as conn:
        after = {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("paper_orders", "paper_positions", "paper_settlements", "paper_equity_curve")
        }
    assert after == before


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


def test_poll_paper_performance_callback_edits_existing_message(tmp_path):
    console = TelegramConsole(_config(tmp_path, admins=(123,)), paper_intelligence_provider=_paper_report)
    calls = []

    def fake_request(method, params):
        calls.append((method, params))
        if method == "getUpdates":
            return {"result": [_callback_update("report_paper", message_id=789)]}
        if method == "editMessageText":
            return {"ok": True, "result": {"message_id": params["message_id"]}}
        return {"ok": True}

    console._telegram_request = fake_request

    next_offset = console.poll_once(timeout=1)

    assert next_offset == 101
    assert [method for method, _params in calls] == ["getUpdates", "answerCallbackQuery", "editMessageText"]
    edit_params = calls[2][1]
    assert edit_params["chat_id"] == 456
    assert edit_params["message_id"] == 789
    assert "Paper Performance" in edit_params["text"]
    assert "sendMessage" not in [method for method, _params in calls]


def test_poll_back_callback_edits_same_message(tmp_path):
    console = TelegramConsole(_config(tmp_path, admins=(123,)))
    calls = []

    def fake_request(method, params):
        calls.append((method, params))
        if method == "getUpdates":
            return {"result": [_callback_update("menu_main", message_id=789)]}
        if method == "editMessageText":
            return {"ok": True, "result": {"message_id": params["message_id"]}}
        return {"ok": True}

    console._telegram_request = fake_request

    console.poll_once(timeout=1)

    assert [method for method, _params in calls] == ["getUpdates", "answerCallbackQuery", "editMessageText"]
    assert calls[2][1]["message_id"] == 789
    assert calls[2][1]["text"] == "Polylens Control Console\nChoose a category."


def test_poll_navigation_fallback_sends_only_if_edit_fails(tmp_path):
    console = TelegramConsole(_config(tmp_path, admins=(123,)))
    calls = []

    def fake_request(method, params):
        calls.append((method, params))
        if method == "getUpdates":
            return {"result": [_callback_update("menu_system", message_id=789)]}
        if method == "editMessageText":
            raise RuntimeError("edit failed")
        if method == "sendMessage":
            return {"ok": True, "result": {"message_id": 990}}
        return {"ok": True}

    console._telegram_request = fake_request

    console.poll_once(timeout=1)

    assert [method for method, _params in calls] == ["getUpdates", "answerCallbackQuery", "editMessageText", "sendMessage"]
    assert calls[3][1]["chat_id"] == 456
    assert calls[3][1]["text"] == "System\nSafe service, paper, and risk status."
    assert console._active_console_messages[(456, 123)] == 990


def test_console_command_reuses_active_console_message(tmp_path):
    console = TelegramConsole(_config(tmp_path, admins=(123,)))
    calls = []
    updates = [
        {
            "update_id": 100,
            "message": {"text": "/start", "chat": {"id": 456}, "from": {"id": 123}},
        },
        {
            "update_id": 101,
            "message": {"text": "/console", "chat": {"id": 456}, "from": {"id": 123}},
        },
    ]

    def fake_request(method, params):
        calls.append((method, params))
        if method == "getUpdates":
            return {"result": updates}
        if method == "sendMessage":
            return {"ok": True, "result": {"message_id": 777}}
        if method == "editMessageText":
            return {"ok": True, "result": {"message_id": params["message_id"]}}
        return {"ok": True}

    console._telegram_request = fake_request

    next_offset = console.poll_once(timeout=1)

    assert next_offset == 102
    assert [method for method, _params in calls] == ["getUpdates", "sendMessage", "editMessageText"]
    assert calls[1][1]["text"].startswith("Polylens Telegram console")
    assert calls[2][1]["message_id"] == 777


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


def test_answer_callback_http_error_does_not_stop_later_updates(tmp_path, caplog):
    token = "123456:super-secret"
    console = TelegramConsole(_config(tmp_path, admins=(123,), token=token))
    updates = {
        "result": [
            {
                "update_id": 1,
                "callback_query": {
                    "id": "expired-callback",
                    "data": "menu_system",
                    "from": {"id": 123},
                    "message": {"message_id": 10, "chat": {"id": 456}},
                },
            },
            {
                "update_id": 2,
                "callback_query": {
                    "id": "fresh-callback",
                    "data": "health",
                    "from": {"id": 123},
                    "message": {"message_id": 11, "chat": {"id": 456}},
                },
            },
        ]
    }
    calls = []

    def fake_request(method, params):
        calls.append((method, params))
        if method == "getUpdates":
            return updates
        if method == "answerCallbackQuery" and params["callback_query_id"] == "expired-callback":
            raise HTTPError(
                url="https://api.telegram.org/bot123456:super-secret/answerCallbackQuery",
                code=400,
                msg="Bad Request: query is too old",
                hdrs=None,
                fp=None,
            )
        return {"ok": True}

    console._telegram_request = fake_request

    with caplog.at_level("WARNING"):
        next_offset = console.poll_once()

    assert next_offset == 3
    assert [method for method, _params in calls].count("answerCallbackQuery") == 2
    assert [method for method, _params in calls].count("editMessageText") == 2
    assert "answerCallbackQuery failed" in caplog.text
    assert token not in caplog.text


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
        "src.integrations.telegram_notifications.paper_trading_intelligence",
        lambda **kwargs: {
            **_empty_paper_report(),
            "daily_pnl": 3.25,
            "pnl_7d": 7.5,
            "total_pnl": 13.25,
            "open_positions_count": 1,
            "closed_positions_count": 4,
            "win_rate": 0.75,
            "trade_count": 5,
        },
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
    assert "Daily PnL: +$3.25" in text
    assert "7D PnL: +$7.50" in text
    assert "Total PnL: +$13.25" in text
