from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from urllib.error import HTTPError

import pytest

from src.analysis.paper_intelligence import paper_trading_intelligence
from src.analysis.paper_trading_engine import init_paper_trading_db
from src.integrations.telegram_console import (
    MAX_TELEGRAM_TEXT,
    TELEGRAM_HTML_PARSE_MODE,
    TIMEZONE,
    TelegramConsole,
    TelegramConsoleConfig,
    TelegramConsoleConfigError,
    TelegramResponse,
    escape_telegram_html,
    format_count_metric,
    format_local_time,
    format_pct_metric,
    format_trend_suffix,
    render_dashboard_text,
    render_page_timestamp,
    render_table_section,
    select_telegram_parse_mode,
    status_icon,
    visual_status_label,
    visual_trend_label,
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
DASHBOARD_NOW = datetime(2026, 6, 26, 12, 34, 56, tzinfo=timezone.utc)
DASHBOARD_UPDATED = "Updated:\n8:34:56 AM EDT"


def _config(tmp_path, admins=(123,), token="secret-token") -> TelegramConsoleConfig:
    return TelegramConsoleConfig(
        bot_token=token,
        admin_user_ids=frozenset(admins),
        audit_db_path=str(tmp_path / "telegram.db"),
        trader_signal_db_path=str(tmp_path / "signals.db"),
        paper_db_path=str(tmp_path / "paper.db"),
        short_crypto_paper_db_path=str(tmp_path / "short_crypto_paper.db"),
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


def _dashboard_health():
    return {
        "status": "healthy",
        "success_rate": 1.0,
        "stale_cycles": [],
        "failures": [],
        "checked_at": "2026-06-26T12:34:40Z",
        "service_uptime_anchor": "2026-06-26T10:00:00Z",
        "cycles": [
            {
                "cycle": "signals",
                "last_run_at": "2026-06-26T12:32:56Z",
                "last_status": "success",
            }
        ],
    }


def _dashboard_signals():
    return {
        "status": "healthy",
        "signal_count": 300,
        "scored_count": 300,
        "recommendation_count": 12,
        "performance_count": 20,
        "validation_accuracy": 0.562,
        "last_cycle": {
            "cycle_timestamp": "2026-06-26T12:31:56Z",
            "signals_generated": 124,
            "recommendations_generated": 7,
        },
    }


def _dashboard_paper_health():
    return {
        "status": "healthy",
        "last_run": "2026-06-26T12:30:56Z",
        "runs_24h": 8,
        "positions_open": 1,
        "positions_closed": 47,
        "equity": 1103.55,
    }


def _dashboard_console(tmp_path, **kwargs):
    defaults = {
        "health_provider": _dashboard_health,
        "signals_provider": _dashboard_signals,
        "paper_provider": _dashboard_paper_health,
        "paper_intelligence_provider": _paper_report,
        "risk_provider": lambda: {"halted": False, "active_halts": []},
        "now_provider": lambda: DASHBOARD_NOW,
    }
    defaults.update(kwargs)
    return TelegramConsole(_config(tmp_path, admins=(123,)), **defaults)


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
    assert {"nav_performance", "nav_intelligence", "nav_wallets", "nav_reports", "nav_system", "refresh_page"}.issubset(_callback_ids(response.reply_markup))


def test_start_returns_dashboard_reply_markup(tmp_path):
    console = _dashboard_console(tmp_path)

    response = console.handle_text(123, "/start")

    assert "📊 Polylens Mission Control" in response
    assert response.reply_markup is not None
    assert "nav_system" in _callback_ids(response.reply_markup)
    assert "refresh_page" in _callback_ids(response.reply_markup)


def test_dashboard_rendering_includes_statuses_metrics_and_timestamp(tmp_path):
    console = _dashboard_console(tmp_path)

    response = console.handle_text(123, "/console")

    assert "📊 Polylens Mission Control" in response
    assert "🟢 Healthy" in response
    assert "📊 Executive Summary" in response
    assert "Trading Mode" in response
    assert "🟢 Paper Only" in response
    assert "Signals Today" in response
    assert "124" in response
    assert "Paper Win Rate" in response
    assert "58.3%" in response
    assert "🟢 Action Center" in response
    assert "No action required." in response
    assert "Today's Snapshot" in response
    assert "Last Cycle" in response
    assert "2m ago" in response
    assert "Overall Health" in response
    assert "100/100" in response
    assert "Updated:\n8:34:56 AM EDT" in response
    assert "<pre>" in response.text
    assert response.parse_mode == TELEGRAM_HTML_PARSE_MODE


def test_dashboard_status_icon_rendering():
    assert status_icon("healthy") == "🟢"
    assert status_icon("degraded") == "🟡"
    assert status_icon("unhealthy") == "🔴"


def test_dashboard_metric_formatting():
    assert format_count_metric(1240) == "1,240"
    assert format_count_metric(None) == "0"
    assert format_pct_metric(0.562) == "56.2%"
    assert format_pct_metric(56.2) == "56.2%"


def test_render_dashboard_text_uses_supplied_utc_timestamp():
    text = render_dashboard_text(
        config=TelegramConsoleConfig(bot_token="token", admin_user_ids=frozenset({123})),
        health=_dashboard_health(),
        signals=_dashboard_signals(),
        paper_health=_dashboard_paper_health(),
        paper=_paper_report(),
        risk={"halted": False},
        now=DASHBOARD_NOW,
    )

    assert text.endswith(DASHBOARD_UPDATED)


def test_menu_navigation_to_system_menu(tmp_path):
    console = _dashboard_console(tmp_path)

    response = console.handle_callback(123, "menu_system")

    assert response.text.splitlines()[0] == "🏠 Home › ⚙️ System"
    assert "Updated:\n8:34:56 AM EDT" in response
    assert response.reply_markup is not None
    assert {"quick_disk", "quick_memory", "quick_services", "nav_home", "nav_back", "refresh_page"}.issubset(_callback_ids(response.reply_markup))


def test_back_navigation_returns_main_menu(tmp_path):
    console = _dashboard_console(tmp_path)

    response = console.handle_callback(123, "menu_main")

    assert "📊 Polylens Mission Control" in response
    assert "Updated:\n8:34:56 AM EDT" in response
    assert response.reply_markup is not None
    assert {"nav_performance", "nav_intelligence", "nav_wallets", "nav_reports", "nav_system", "refresh_page"}.issubset(_callback_ids(response.reply_markup))


def test_v3_home_submenu_back_and_home_flow(tmp_path):
    console = _dashboard_console(tmp_path)

    home = console.handle_console_callback(456, 123, "nav_performance")
    drilldown = console.handle_console_callback(456, 123, "drill_paper_trades")
    back = console.handle_console_callback(456, 123, "nav_back")
    final_home = console.handle_console_callback(456, 123, "nav_home")

    assert "📈 Performance" in home.text
    assert "📄 Paper Trades" in drilldown.text
    assert "📈 Performance" in back.text
    assert "📊 Polylens Mission Control" in final_home.text


@pytest.mark.parametrize(
    ("callback_id", "heading"),
    [
        ("nav_performance", "📈 Performance"),
        ("nav_intelligence", "🧠 Intelligence"),
        ("nav_wallets", "👛 Wallets"),
        ("nav_reports", "📊 Reports"),
        ("nav_system", "⚙️ System"),
    ],
)
def test_v3_every_page_has_timestamp_and_refresh_preserves_page(tmp_path, callback_id, heading):
    console = _dashboard_console(
        tmp_path,
        top_wallets_provider=lambda: [{"wallet": VALID_WALLET, "watch_score": 91, "classification": "arbitrage"}],
    )

    page = console.handle_console_callback(456, 123, callback_id)
    refreshed = console.handle_console_callback(456, 123, "refresh_page")

    assert heading in page.text
    assert page.text.endswith(DASHBOARD_UPDATED)
    assert heading in refreshed.text
    assert refreshed.text.endswith(DASHBOARD_UPDATED)


@pytest.mark.parametrize(
    ("parent_callback", "drill_callback", "heading"),
    [
        ("nav_performance", "drill_paper_trades", "📈 Paper Trades"),
        ("nav_intelligence", "drill_signals_today", "🧠 Signals Today"),
        ("nav_wallets", "drill_wallet_stats", "👛 Wallet Stats"),
        ("nav_intelligence", "drill_validation", "🧠 Validation Accuracy"),
    ],
)
def test_v3_drilldown_navigation_returns_to_immediate_parent(tmp_path, parent_callback, drill_callback, heading):
    console = _dashboard_console(
        tmp_path,
        top_wallets_provider=lambda: [{"wallet": VALID_WALLET, "watch_score": 91, "classification": "arbitrage"}],
    )

    parent = console.handle_console_callback(456, 123, parent_callback)
    drilldown = console.handle_console_callback(456, 123, drill_callback)
    back = console.handle_console_callback(456, 123, "nav_back")

    assert heading in drilldown.text
    assert parent.text.splitlines()[0] == back.text.splitlines()[0]


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
    console = _dashboard_console(tmp_path)

    response = console.handle_callback(123, "menu_reports")

    assert response.reply_markup is not None
    assert {"quick_report_daily", "quick_report_weekly", "quick_report_monthly", "nav_home", "nav_back", "refresh_page"}.issubset(_callback_ids(response.reply_markup))


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


def test_v3_poll_navigation_edits_same_message_without_send(tmp_path):
    console = _dashboard_console(tmp_path)
    calls = []

    def fake_request(method, params):
        calls.append((method, params))
        if method == "getUpdates":
            return {"result": [_callback_update("nav_intelligence", message_id=789)]}
        if method == "editMessageText":
            return {"ok": True, "result": {"message_id": params["message_id"]}}
        return {"ok": True}

    console._telegram_request = fake_request

    console.poll_once(timeout=1)

    assert [method for method, _params in calls] == ["getUpdates", "answerCallbackQuery", "editMessageText"]
    assert calls[-1][1]["message_id"] == 789
    assert "🧠 Intelligence" in calls[-1][1]["text"]
    assert "sendMessage" not in [method for method, _params in calls]


def test_poll_back_callback_edits_same_message(tmp_path):
    console = _dashboard_console(tmp_path)
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
    assert "📊 Polylens Mission Control" in calls[2][1]["text"]
    assert "Updated:\n8:34:56 AM EDT" in calls[2][1]["text"]


def test_poll_refresh_callback_edits_existing_message(tmp_path):
    console = _dashboard_console(tmp_path)
    calls = []

    def fake_request(method, params):
        calls.append((method, params))
        if method == "getUpdates":
            return {"result": [_callback_update("dashboard_refresh", message_id=789)]}
        if method == "editMessageText":
            return {"ok": True, "result": {"message_id": params["message_id"]}}
        return {"ok": True}

    console._telegram_request = fake_request

    console.poll_once(timeout=1)

    assert [method for method, _params in calls] == ["getUpdates", "answerCallbackQuery", "editMessageText"]
    assert calls[2][1]["message_id"] == 789
    assert "📊 Polylens Mission Control" in calls[2][1]["text"]
    assert "sendMessage" not in [method for method, _params in calls]


def test_poll_refresh_callback_never_sends_new_message_if_edit_fails(tmp_path):
    console = _dashboard_console(tmp_path)
    calls = []

    def fake_request(method, params):
        calls.append((method, params))
        if method == "getUpdates":
            return {"result": [_callback_update("dashboard_refresh", message_id=789)]}
        if method == "editMessageText":
            raise RuntimeError("edit failed")
        return {"ok": True}

    console._telegram_request = fake_request

    console.poll_once(timeout=1)

    assert [method for method, _params in calls] == ["getUpdates", "answerCallbackQuery", "editMessageText"]
    assert "sendMessage" not in [method for method, _params in calls]


def test_poll_page_navigation_never_sends_if_edit_fails(tmp_path):
    console = _dashboard_console(tmp_path)
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

    assert [method for method, _params in calls] == ["getUpdates", "answerCallbackQuery", "editMessageText"]
    assert "sendMessage" not in [method for method, _params in calls]


def test_console_command_reuses_active_console_message(tmp_path):
    console = _dashboard_console(tmp_path)
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
    assert "📊 Polylens Mission Control" in calls[1][1]["text"]
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


def test_v4_breadcrumb_and_persistent_bottom_navigation(tmp_path):
    console = _dashboard_console(tmp_path)

    home = console.handle_console_callback(456, 123, "nav_home")
    performance = console.handle_console_callback(456, 123, "nav_performance")

    assert home.text.splitlines()[0] == "🏠 Home"
    assert performance.text.splitlines()[0] == "🏠 Home › 📈 Performance"
    assert [button["callback_data"] for button in home.reply_markup["inline_keyboard"][-1]] == ["nav_home", "refresh_page"]
    assert [button["callback_data"] for button in performance.reply_markup["inline_keyboard"][-1]] == ["nav_home", "nav_back", "refresh_page"]


def test_v4_favorites_are_user_scoped_and_rendered_below_breadcrumb(tmp_path):
    console = _dashboard_console(tmp_path)

    console.handle_console_callback(456, 123, "nav_performance")
    favorited = console.handle_console_callback(456, 123, "favorite_toggle")
    wallet_stats = console.handle_console_callback(456, 123, "nav_wallets")
    wallet_stats = console.handle_console_callback(456, 123, "quick_wallet_stats")
    other_user = console.handle_console_callback(456, 999, "nav_performance")

    assert "⭐ Favorites\n• 📈 Performance" in favorited.text
    assert "⭐ Favorites\n• 📈 Performance" in wallet_stats.text
    assert "fav_performance" in _callback_ids(wallet_stats.reply_markup)
    assert other_user.text == "unauthorized"


def test_v4_recent_pages_back_stack_and_home_clear_history(tmp_path):
    console = _dashboard_console(tmp_path)

    console.handle_console_callback(456, 123, "nav_performance")
    paper = console.handle_console_callback(456, 123, "quick_paper_trades")
    back = console.handle_console_callback(456, 123, "nav_back")
    home = console.handle_console_callback(456, 123, "nav_home")

    assert paper.text.splitlines()[0] == "🏠 Home › 📈 Performance › 📄 Paper Trades"
    assert back.text.splitlines()[0] == "🏠 Home › 📈 Performance"
    assert "🕒 Recent\n• 📄 Paper Trades" in back.text
    assert home.text.splitlines()[0] == "🏠 Home"
    assert console._v4_navigation_states[123].history == []


@pytest.mark.parametrize(
    ("parent_callback", "quick_callback", "heading"),
    [
        ("nav_wallets", "quick_wallet_stats", "Wallet Stats"),
        ("nav_reports", "quick_report_daily", "📅 Daily"),
        ("nav_system", "quick_services", "⚙️ Services"),
    ],
)
def test_v4_context_actions_navigate_to_read_only_pages(tmp_path, parent_callback, quick_callback, heading):
    console = _dashboard_console(tmp_path)

    console.handle_console_callback(456, 123, parent_callback)
    response = console.handle_console_callback(456, 123, quick_callback)

    assert heading in response.text
    assert "Updated:\n8:34:56 AM EDT" in response.text
    assert {"nav_home", "nav_back", "refresh_page"}.issubset(_callback_ids(response.reply_markup))


def test_v4_refresh_preserves_page_state_across_callback_contexts(tmp_path):
    console = _dashboard_console(tmp_path)

    console.handle_console_callback(456, 123, "nav_wallets")
    console.handle_console_callback(456, 123, "quick_wallet_stats")
    refreshed = console.handle_console_callback(999, 123, "refresh_page")

    assert refreshed.text.splitlines()[0] == "🏠 Home › 👛 Wallets › Wallet Stats"
    assert console._v4_navigation_states[123].current_page == "wallet_stats"


def test_v4_quick_navigation_edits_the_existing_message_only(tmp_path):
    console = _dashboard_console(tmp_path)
    calls = []
    updates = [
        _callback_update("nav_wallets", message_id=789),
        _callback_update("quick_wallet_stats", message_id=789),
    ]

    def fake_request(method, params):
        calls.append((method, params))
        if method == "getUpdates":
            return {"result": updates}
        if method == "editMessageText":
            return {"ok": True, "result": {"message_id": params["message_id"]}}
        return {"ok": True}

    console._telegram_request = fake_request
    console.poll_once(timeout=1)

    assert [method for method, _params in calls] == [
        "getUpdates", "answerCallbackQuery", "editMessageText", "answerCallbackQuery", "editMessageText"
    ]
    assert all(params.get("message_id") == 789 for method, params in calls if method == "editMessageText")
    assert "sendMessage" not in [method for method, _params in calls]


def test_v4_render_timing_helper_uses_cached_read_only_snapshot(tmp_path):
    from src.integrations.telegram_console import render_timing_ms

    console = _dashboard_console(tmp_path)
    console.handle_console_callback(456, 123, "nav_performance")

    response, elapsed_ms = render_timing_ms(lambda: console.handle_console_callback(456, 123, "refresh_page"))

    assert "📈 Performance" in response.text
    assert elapsed_ms < 150

def test_v4_favorites_do_not_leak_between_allowed_users(tmp_path):
    console = _dashboard_console(tmp_path)
    console.config = _config(tmp_path, admins=(123, 456))

    console.handle_console_callback(111, 123, "nav_performance")
    console.handle_console_callback(111, 123, "favorite_toggle")
    other_user = console.handle_console_callback(222, 456, "nav_performance")

    assert "⭐ Favorites" in console.handle_console_callback(111, 123, "refresh_page").text
    assert "⭐ Favorites" not in other_user.text


def test_escape_telegram_html_escapes_special_characters():
    assert escape_telegram_html("a & b <tag> $1.50 (demo)") == "a &amp; b &lt;tag&gt; $1.50 (demo)"


def test_select_telegram_parse_mode_prefers_html_for_console():
    assert select_telegram_parse_mode(use_html=True) == TELEGRAM_HTML_PARSE_MODE
    assert select_telegram_parse_mode(use_html=False) is None


def test_render_table_section_aligns_columns():
    rendered = render_table_section(
        "System Health",
        [
            ("Wallet Autonomy", "🟢 Running"),
            ("Health Timer", "🟢 2m ago"),
        ],
        columns=("Component", "Status"),
    )

    assert rendered.startswith("<pre>System Health")
    assert "Component" in rendered
    assert "Status" in rendered
    assert "Wallet Autonomy" in rendered
    assert "Health Timer" in rendered
    assert "Wallet Autonomy  🟢 Running" in rendered
    assert "Health Timer     🟢 2m ago" in rendered
    assert rendered.endswith("</pre>")


def test_home_dashboard_uses_grouped_pre_tables():
    text = render_dashboard_text(
        config=TelegramConsoleConfig(bot_token="token", admin_user_ids=frozenset({123})),
        health=_dashboard_health(),
        signals=_dashboard_signals(),
        paper_health=_dashboard_paper_health(),
        paper=_paper_report(),
        risk={"halted": False},
        now=DASHBOARD_NOW,
    )

    assert "<pre>📊 Executive Summary" in text
    assert "<pre>🟢 Action Center" in text
    assert "<pre>Today's Snapshot" in text
    assert "🟢 Paper Only" in text
    assert "Signals Today" in text
    assert "124" in text


def test_performance_page_uses_grouped_sections(tmp_path):
    console = _dashboard_console(tmp_path)

    response = console.handle_console_callback(456, 123, "nav_performance")

    assert "<pre>Performance" in response.text
    assert "<pre>Top Performers" in response.text
    assert "<pre>Portfolio" in response.text
    assert "<pre>PnL" in response.text
    assert "<pre>Execution Quality" in response.text
    assert "Win Rate" in response.text
    assert "🏆 Winning Trades" in response.text
    assert response.parse_mode == TELEGRAM_HTML_PARSE_MODE


def test_wallets_page_shows_overview_and_top_wallets(tmp_path):
    console = _dashboard_console(
        tmp_path,
        top_wallets_provider=lambda: [
            {"wallet": VALID_WALLET, "watch_score": 91, "classification": "arbitrage"},
            {"wallet": "0xabc123456789012345678901234567890123456", "watch_score": 72, "classification": "momentum"},
        ],
    )

    response = console.handle_console_callback(456, 123, "nav_wallets")

    assert "<pre>Wallet Health" in response.text
    assert "🟢 Active" in response.text
    assert "<pre>Ranked Wallets" in response.text
    assert "0x7af3...83fe" in response.text


def test_wallet_stats_page_uses_table_layout(tmp_path):
    console = _dashboard_console(
        tmp_path,
        top_wallets_provider=lambda: [{"wallet": VALID_WALLET, "watch_score": 91, "classification": "arbitrage"}],
    )

    console.handle_console_callback(456, 123, "nav_wallets")
    response = console.handle_console_callback(456, 123, "quick_wallet_stats")

    assert "Ranked Wallets" in response.text
    assert "0x7af3...83fe" in response.text
    assert "🟢 Active" in response.text


def test_system_page_shows_services_and_resources(tmp_path):
    health = _dashboard_health()
    health["disk_usage_pct"] = 0.42
    health["memory_usage_pct"] = 0.61
    console = _dashboard_console(tmp_path, health_provider=lambda: health)

    response = console.handle_console_callback(456, 123, "nav_system")

    assert "<pre>Services" in response.text
    assert "<pre>Health" in response.text
    assert "<pre>Resources" in response.text
    assert "Disk" in response.text
    assert "Memory" in response.text
    assert "Last Cycle" in response.text


def test_reports_page_shows_daily_weekly_monthly_sections(tmp_path):
    console = _dashboard_console(tmp_path)

    response = console.handle_console_callback(456, 123, "nav_reports")

    assert "<pre>Daily Summary" in response.text
    assert "<pre>Weekly Summary" in response.text
    assert "<pre>Monthly Summary" in response.text
    assert "7D PnL" in response.text
    assert "Total PnL" in response.text


def test_console_navigation_includes_html_parse_mode_in_edit(tmp_path):
    console = _dashboard_console(tmp_path)
    calls = []

    def fake_request(method, params):
        calls.append((method, params))
        if method == "getUpdates":
            return {"result": [_callback_update("refresh_page", message_id=789)]}
        if method == "editMessageText":
            return {"ok": True, "result": {"message_id": params["message_id"]}}
        return {"ok": True}

    console._telegram_request = fake_request
    console.poll_once(timeout=1)

    edit_params = calls[-1][1]
    assert edit_params["parse_mode"] == TELEGRAM_HTML_PARSE_MODE
    assert "sendMessage" not in [method for method, _params in calls]


def test_format_local_time_uses_edt_during_daylight_saving():
    dt = datetime(2026, 6, 26, 0, 13, 1, tzinfo=timezone.utc)

    assert format_local_time(dt) == "8:13:01 PM EDT"


def test_format_local_time_uses_est_during_standard_time():
    dt = datetime(2026, 1, 15, 17, 13, 1, tzinfo=timezone.utc)

    assert format_local_time(dt) == "12:13:01 PM EST"


def test_format_local_time_respects_dst_spring_forward_boundary():
    before = datetime(2026, 3, 8, 6, 59, 0, tzinfo=timezone.utc)
    after = datetime(2026, 3, 8, 7, 30, 0, tzinfo=timezone.utc)

    assert format_local_time(before) == "1:59:00 AM EST"
    assert format_local_time(after) == "3:30:00 AM EDT"


def test_format_local_time_respects_dst_fall_back_boundary():
    before = datetime(2026, 11, 1, 5, 59, 0, tzinfo=timezone.utc)
    after = datetime(2026, 11, 1, 6, 30, 0, tzinfo=timezone.utc)

    assert format_local_time(before) == "1:59:00 AM EDT"
    assert format_local_time(after) == "1:30:00 AM EST"


def test_render_page_timestamp_uses_new_york_timezone():
    assert render_page_timestamp(DASHBOARD_NOW) == DASHBOARD_UPDATED


def test_home_dashboard_footer_uses_local_timezone(tmp_path):
    console = _dashboard_console(tmp_path)

    response = console.handle_text(123, "/console")

    assert response.text.endswith(DASHBOARD_UPDATED)
    assert str(TIMEZONE) == "America/New_York"


def test_reports_page_footer_uses_local_timezone(tmp_path):
    console = _dashboard_console(tmp_path)

    response = console.handle_console_callback(456, 123, "nav_reports")

    assert response.text.endswith(DASHBOARD_UPDATED)


def test_relative_times_remain_unchanged_with_local_footer(tmp_path):
    console = _dashboard_console(tmp_path)

    response = console.handle_text(123, "/console")

    assert "Last Cycle" in response.text
    assert "2m ago" in response.text
    assert "Today's Snapshot" in response.text
    assert response.text.endswith(DASHBOARD_UPDATED)


def test_v5_visual_status_labels():
    assert visual_status_label("healthy") == "🟢 Healthy"
    assert visual_status_label("degraded") == "🟡 Warning"
    assert visual_status_label("offline") == "⚪ Offline"
    assert visual_status_label("critical") == "🔴 Critical"
    assert visual_status_label("info") == "🔵 Information"


def test_v5_visual_trend_labels():
    assert visual_trend_label("improving") == "📈 Improving"
    assert visual_trend_label("declining") == "📉 Declining"
    assert visual_trend_label("stable") == "➡ Stable"


def test_v5_trend_suffix_renders_deltas():
    assert format_trend_suffix(142, 124, integer=True) == " ▲ +18"
    assert format_trend_suffix(0.618, 0.595, pct_points=True) == " ▲ +2.3%"
    assert format_trend_suffix(10, 10, integer=True) == " ➡"


def test_v5_trend_suffix_omits_missing_comparison():
    assert format_trend_suffix(142, None, integer=True) == ""


def test_v5_wallet_health_card(tmp_path):
    console = _dashboard_console(
        tmp_path,
        top_wallets_provider=lambda: [
            {"wallet": VALID_WALLET, "watch_score": 91, "state": "promoted"},
            {"wallet": "0xabc123456789012345678901234567890123456", "watch_score": 72, "state": "probation"},
            {"wallet": "0xdef123456789012345678901234567890123456", "watch_score": 55, "state": "retired"},
        ],
    )

    response = console.handle_console_callback(456, 123, "nav_wallets")

    assert "⭐ Promoted" in response.text
    assert "🟡 Probation" in response.text
    assert "⚪ Retired" in response.text
    assert "Top Wallet" in response.text


def test_v5_performance_dashboard_formatting(tmp_path):
    payload = _paper_report()
    payload["previous_trade_count"] = 36
    payload["closed_positions"] = [{"status": "WIN"} for _ in range(10)] + [{"status": "LOSS"} for _ in range(5)]
    console = _dashboard_console(
        tmp_path,
        paper_intelligence_provider=lambda: payload,
        signals_provider=lambda: {
            **_dashboard_signals(),
            "previous_signals_today": 106,
            "previous_win_rate": 0.55,
        },
    )

    response = console.handle_console_callback(456, 123, "nav_performance")

    assert "📈 Signals Today" in response.text
    assert "▲ +18" in response.text
    assert "🏆 Winning Trades" in response.text
    assert "📉 Losing Trades" in response.text
    assert "🥇 Early Entry" in response.text


def test_v5_reports_dashboard_formatting(tmp_path):
    console = _dashboard_console(tmp_path)

    response = console.handle_console_callback(456, 123, "nav_reports")

    assert "Signals Generated" in response.text
    assert "Best Strategy" in response.text
    assert "🥇 Early Entry" in response.text


def test_v5_service_status_formatting(tmp_path):
    health = _dashboard_health()
    health["cpu_usage_pct"] = 0.12
    health["memory_free_gb"] = 6.8
    health["disk_usage_pct"] = 0.61
    console = _dashboard_console(tmp_path, health_provider=lambda: health)

    response = console.handle_console_callback(456, 123, "nav_system")

    assert "🟢 Mission Control" in response.text
    assert "CPU" in response.text
    assert "6.8 GB Free" in response.text


def test_v5_refresh_still_edits_existing_message_only(tmp_path):
    console = _dashboard_console(tmp_path)
    calls = []

    def fake_request(method, params):
        calls.append((method, params))
        if method == "getUpdates":
            return {"result": [_callback_update("refresh_page", message_id=789)]}
        if method == "editMessageText":
            return {"ok": True, "result": {"message_id": params["message_id"]}}
        return {"ok": True}

    console._telegram_request = fake_request
    console.poll_once(timeout=1)

    assert [method for method, _params in calls] == ["getUpdates", "answerCallbackQuery", "editMessageText"]
    assert "sendMessage" not in [method for method, _params in calls]
    assert "📊 Executive Summary" in calls[-1][1]["text"]


def test_v6_executive_summary_renders_on_home(tmp_path):
    console = _dashboard_console(tmp_path)

    response = console.handle_text(123, "/console")

    assert "📊 Executive Summary" in response.text
    assert "System Status" in response.text
    assert "Trading Mode" in response.text
    assert "Alerts" in response.text


def test_v6_action_center_shows_no_action_required(tmp_path):
    console = _dashboard_console(tmp_path)

    response = console.handle_text(123, "/console")

    assert "🟢 Action Center" in response.text
    assert "No action required." in response.text


def test_v6_action_center_shows_alerts_from_health_data(tmp_path):
    health = _dashboard_health()
    health["disk_usage_pct"] = 0.92
    health["stale_cycles"] = ["signals"]
    console = _dashboard_console(
        tmp_path,
        health_provider=lambda: health,
        signals_provider=lambda: {
            **_dashboard_signals(),
            "previous_validation_accuracy": 0.62,
            "validation_accuracy": 0.562,
        },
    )

    response = console.handle_text(123, "/console")

    assert "⚠ Action Center" in response.text
    assert "Disk usage above" in response.text
    assert "Validation accuracy decreased" in response.text


def test_v6_top_movers_and_snapshot_formatting(tmp_path):
    payload = _paper_report()
    payload["best_session"] = "morning"
    payload["biggest_improvement"] = {"strategy": "momentum", "delta": 0.061}
    payload["largest_decline"] = {"strategy": "contrarian", "delta": -0.028}
    console = _dashboard_console(
        tmp_path,
        paper_intelligence_provider=lambda: payload,
        top_wallets_provider=lambda: [{"wallet": VALID_WALLET, "watch_score": 91, "classification": "arbitrage"}],
    )

    response = console.handle_text(123, "/console")

    assert "Top Movers" in response.text
    assert "🥇 Early Entry" in response.text
    assert "👛 Top Wallet" in response.text
    assert "0x7af3...83fe" in response.text
    assert "📈 Biggest Improvement" in response.text
    assert "Momentum +6.1%" in response.text
    assert "📉 Largest Decline" in response.text
    assert "Contrarian -2.8%" in response.text
    assert "Today's Snapshot" in response.text
    assert "Best Session" in response.text
    assert "Morning" in response.text


def test_v6_omits_unavailable_top_mover_fields(tmp_path):
    console = _dashboard_console(tmp_path, paper_intelligence_provider=_empty_paper_report)

    response = console.handle_text(123, "/console")

    assert "Top Movers" not in response.text
    assert "Best Session" not in response.text


def test_v6_health_score_renders_from_success_rate(tmp_path):
    console = _dashboard_console(tmp_path)

    response = console.handle_text(123, "/console")

    assert "Overall Health" in response.text
    assert "100/100" in response.text
    assert "🟢 Excellent" in response.text


def test_v6_refresh_still_edits_same_message_only(tmp_path):
    console = _dashboard_console(tmp_path)
    calls = []

    def fake_request(method, params):
        calls.append((method, params))
        if method == "getUpdates":
            return {"result": [_callback_update("refresh_page", message_id=789)]}
        if method == "editMessageText":
            return {"ok": True, "result": {"message_id": params["message_id"]}}
        return {"ok": True}

    console._telegram_request = fake_request
    console.poll_once(timeout=1)

    assert [method for method, _params in calls] == ["getUpdates", "answerCallbackQuery", "editMessageText"]
    assert calls[-1][1]["message_id"] == 789
    assert "sendMessage" not in [method for method, _params in calls]
    assert "📊 Executive Summary" in calls[-1][1]["text"]
