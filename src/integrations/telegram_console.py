from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from src.analysis.paper_trading_engine import DEFAULT_PAPER_TRADING_DB
from src.analysis.paper_intelligence import paper_trading_intelligence
from src.analysis.short_crypto_paper import DEFAULT_DB_PATH as DEFAULT_SHORT_CRYPTO_PAPER_DB
from src.analysis.paper_trading_service import paper_trading_health
from src.analysis.trader_registry import DEFAULT_TRADERS_DB, top_traders, trader_summary
from src.analysis.trader_signal_engine import DEFAULT_TRADER_SIGNAL_DB, trader_signal_health
from src.intelligence.wallet_service_health import wallet_service_health_summary
from src.integrations.telegram_notifications import (
    format_daily_intelligence_report,
    generate_daily_intelligence_report,
    paper_pnl_report_text,
    paper_performance_report_text,
    outcome_validation_report_text,
    portfolio_analytics_report_text,
    paper_positions_report_text,
    paper_recent_report_text,
    paper_strategies_report_text,
    polymarket_analytics_wallet_buttons,
    signal_summary_report_text,
    wallet_summary_link_buttons,
    wallet_summary_report_text,
)
from src.sqlite_utils import closing_connection
from src.trading.kill_switch import DEFAULT_READINESS_DB, KillSwitch

LOGGER = logging.getLogger(__name__)

DEFAULT_TELEGRAM_AUDIT_DB = DEFAULT_TRADERS_DB
TELEGRAM_API_BASE = "https://api.telegram.org"
MAX_TELEGRAM_TEXT = 3500
TELEGRAM_HTML_PARSE_MODE = "HTML"
COMMANDS = (
    "/start",
    "/console",
    "/help",
    "/status",
    "/health",
    "/signals",
    "/top_wallets",
    "/wallet <address>",
    "/paper_status",
    "/paper_recent",
    "/paper_pnl",
    "/paper_positions",
    "/paper_strategies",
    "/portfolio_analytics",
    "/outcome_validation",
    "/risk",
    "/kill_switch",
)
LIVE_COMMANDS = {
    "/buy",
    "/sell",
    "/order",
    "/trade",
    "/kill_switch",
    "/resume_trading",
}
CALLBACK_COMMANDS = {
    "status": "/status",
    "health": "/health",
    "signals": "/signals",
    "top_wallets": "/top_wallets",
    "paper_status": "/paper_status",
    "risk": "/risk",
    "help": "/help",
    "report_daily": "/report_daily",
    "report_signals": "/report_signals",
    "report_wallets": "/report_wallets",
    "report_paper": "/report_paper",
    "paper_recent": "/paper_recent",
    "paper_pnl": "/paper_pnl",
    "paper_positions": "/paper_positions",
    "paper_strategies": "/paper_strategies",
    "portfolio_analytics": "/portfolio_analytics",
    "outcome_validation": "/outcome_validation",
}
MENU_CALLBACKS = {
    "menu_intelligence": "intelligence",
    "menu_wallets": "wallets",
    "menu_signals": "signals",
    "menu_system": "system",
    "menu_reports": "reports",
}
PAGE_NAV_CALLBACKS = {
    "menu_main": "home",
    "nav_home": "home",
    "nav_performance": "performance",
    "nav_portfolio": "portfolio_analytics",
    "nav_outcome": "outcome_validation",
    "nav_intelligence": "intelligence",
    "nav_wallets": "wallets",
    "nav_reports": "reports",
    "nav_system": "system",
    "menu_intelligence": "intelligence",
    "menu_wallets": "wallets",
    "menu_signals": "intelligence",
    "menu_system": "system",
    "menu_reports": "reports",
}
DRILLDOWN_CALLBACKS = {
    "drill_paper_trades": "paper_trades",
    "drill_signals_today": "signals_today",
    "drill_wallet_stats": "wallet_stats",
    "drill_validation": "validation_accuracy",
}
REFRESH_CALLBACKS = {"dashboard_refresh", "refresh_page"}
BACK_CALLBACKS = {"nav_back"}
LIVE_CALLBACKS = {"buy", "sell", "order", "trade", "kill_switch", "resume_trading"}
WALLET_RE = re.compile(r"^0x[a-fA-F0-9]{40}$")
DASHBOARD_DIVIDER = "━━━━━━━━━━━━━━━━━━"
HOME_PAGE = "home"


class TelegramConsoleConfigError(ValueError):
    """Raised when the Telegram console cannot start safely."""


@dataclass(frozen=True)
class TelegramConsoleConfig:
    bot_token: str
    admin_user_ids: frozenset[int]
    paper_only: bool = True
    live_enabled: bool = False
    audit_db_path: str = DEFAULT_TELEGRAM_AUDIT_DB
    trader_signal_db_path: str = DEFAULT_TRADER_SIGNAL_DB
    paper_db_path: str = DEFAULT_PAPER_TRADING_DB
    short_crypto_paper_db_path: str = DEFAULT_SHORT_CRYPTO_PAPER_DB
    readiness_db_path: str = DEFAULT_READINESS_DB

    @classmethod
    def from_env(cls, *, audit_db_path: str | None = None) -> "TelegramConsoleConfig":
        return cls(
            bot_token=os.environ.get("POLYLENS_TELEGRAM_BOT_TOKEN", ""),
            admin_user_ids=parse_admin_user_ids(os.environ.get("POLYLENS_TELEGRAM_ADMIN_USER_IDS", "")),
            paper_only=_env_bool("POLYLENS_TELEGRAM_PAPER_ONLY", default=True),
            live_enabled=_env_bool("POLYLENS_TELEGRAM_LIVE_ENABLED", default=False),
            audit_db_path=audit_db_path or os.environ.get("POLYLENS_TELEGRAM_AUDIT_DB", DEFAULT_TELEGRAM_AUDIT_DB),
            short_crypto_paper_db_path=os.environ.get("POLYLENS_SHORT_CRYPTO_PAPER_DB", DEFAULT_SHORT_CRYPTO_PAPER_DB),
        )

    def safe_dict(self) -> dict[str, Any]:
        return {
            "bot_token": redact_token(self.bot_token),
            "admin_user_ids": sorted(self.admin_user_ids),
            "paper_only": self.paper_only,
            "live_enabled": self.live_enabled,
            "audit_db_path": self.audit_db_path,
        }


Provider = Callable[[], dict[str, Any]]


@dataclass(frozen=True)
class TelegramResponse:
    text: str
    reply_markup: dict[str, Any] | None = None
    parse_mode: str | None = None

    def __str__(self) -> str:
        return self.text

    def __contains__(self, needle: str) -> bool:
        return needle in self.text

    def __eq__(self, other: object) -> bool:
        if isinstance(other, str):
            return self.text == other
        return super().__eq__(other)


@dataclass(frozen=True)
class ConsolePageContext:
    now: datetime
    health: dict[str, Any]
    signals: dict[str, Any]
    paper_health: dict[str, Any]
    paper: dict[str, Any]
    risk: dict[str, Any]
    top_wallets: list[Any] | None = None


class TelegramConsole:
    def __init__(
        self,
        config: TelegramConsoleConfig,
        *,
        health_provider: Provider | None = None,
        signals_provider: Provider | None = None,
        paper_provider: Provider | None = None,
        paper_intelligence_provider: Provider | None = None,
        risk_provider: Provider | None = None,
        top_wallets_provider: Callable[[], list[Any]] | None = None,
        wallet_provider: Callable[[str], dict[str, Any] | None] | None = None,
        now_provider: Callable[[], datetime] | None = None,
    ) -> None:
        self.config = config
        self.health_provider = health_provider or (lambda: wallet_service_health_summary(traders_db_path=DEFAULT_TRADERS_DB))
        self.signals_provider = signals_provider or (lambda: trader_signal_health(db_path=config.trader_signal_db_path))
        self.paper_provider = paper_provider or (lambda: paper_trading_health(db_path=config.paper_db_path))
        self.paper_intelligence_provider = paper_intelligence_provider or (
            lambda: paper_trading_intelligence(
                db_path=config.paper_db_path,
                short_crypto_db_path=config.short_crypto_paper_db_path,
            )
        )
        self.risk_provider = risk_provider or (lambda: KillSwitch(db_path=config.readiness_db_path).status())
        self.top_wallets_provider = top_wallets_provider or (lambda: top_traders(limit=5, db_path=DEFAULT_TRADERS_DB))
        self.wallet_provider = wallet_provider or (lambda wallet: trader_summary(wallet, db_path=DEFAULT_TRADERS_DB))
        self.now_provider = now_provider or (lambda: datetime.now(timezone.utc))
        self._active_console_messages: dict[tuple[int, int], int] = {}
        self._page_states: dict[tuple[int, int], tuple[str, list[str]]] = {}
        init_telegram_audit_db(config.audit_db_path)

    def validate_startup(self) -> None:
        if not self.config.bot_token:
            raise TelegramConsoleConfigError("telegram bot token missing")
        if not self.config.admin_user_ids:
            raise TelegramConsoleConfigError("telegram admin allowlist missing")

    def _is_allowed(self, telegram_user_id: int) -> bool:
        if not self.config.admin_user_ids:
            return False
        return int(telegram_user_id) in self.config.admin_user_ids

    def handle_text(self, telegram_user_id: int, text: str) -> TelegramResponse:
        raw = str(text or "").strip()
        command, args = split_command(raw)
        return self._handle_command(
            telegram_user_id=telegram_user_id,
            command=command,
            args=args,
            audit_command=command,
        )

    def handle_callback(self, telegram_user_id: int, callback_data: str) -> TelegramResponse:
        callback_id = normalize_callback_id(callback_data)
        if self._is_page_callback(callback_id):
            return self._handle_page_callback(telegram_user_id, callback_id)
        if callback_id in MENU_CALLBACKS:
            return self._handle_menu_callback(telegram_user_id, callback_id)
        if callback_id in LIVE_CALLBACKS:
            command = f"/{callback_id}"
            args = ""
        else:
            command = CALLBACK_COMMANDS.get(callback_id, "")
            args = ""
        if not command:
            return self._handle_unknown_callback(telegram_user_id, callback_id)
        return self._handle_command(
            telegram_user_id=telegram_user_id,
            command=command,
            args=args,
            audit_command=f"callback:{callback_id}",
        )

    def handle_console_callback(
        self,
        chat_id: int,
        telegram_user_id: int,
        callback_data: str,
    ) -> TelegramResponse:
        callback_id = normalize_callback_id(callback_data)
        if self._is_page_callback(callback_id):
            return self._handle_page_callback(
                telegram_user_id,
                callback_id,
                state_key=self._active_console_key(chat_id, telegram_user_id),
            )
        return self.handle_callback(telegram_user_id, callback_data)

    def _is_page_callback(self, callback_id: str) -> bool:
        return callback_id in (
            set(PAGE_NAV_CALLBACKS)
            | set(DRILLDOWN_CALLBACKS)
            | REFRESH_CALLBACKS
            | BACK_CALLBACKS
        )

    def _handle_page_callback(
        self,
        telegram_user_id: int,
        callback_id: str,
        *,
        state_key: tuple[int, int] | None = None,
    ) -> TelegramResponse:
        allowed = self._is_allowed(telegram_user_id)
        response = "admin allowlist missing" if not self.config.admin_user_ids else "unauthorized"
        result_status = "rejected"
        try:
            if allowed:
                page, history = self._page_state(state_key)
                if callback_id in REFRESH_CALLBACKS:
                    target_page = page
                elif callback_id in BACK_CALLBACKS:
                    target_page = history.pop() if history else HOME_PAGE
                else:
                    target_page = PAGE_NAV_CALLBACKS.get(callback_id) or DRILLDOWN_CALLBACKS[callback_id]
                    if target_page == HOME_PAGE:
                        history = []
                    elif target_page != page:
                        history.append(page)
                if state_key is not None:
                    self._page_states[state_key] = (target_page, history)
                response = self._page_response(target_page)
                result_status = "ok"
        except Exception as exc:
            LOGGER.exception("telegram console page failed callback=%s user_id=%s", callback_id, telegram_user_id)
            response = "error: command failed safely"
            result_status = "error"
            error_message = str(exc)
        else:
            error_message = ""
        audit_telegram_command(
            self.config.audit_db_path,
            telegram_user_id=telegram_user_id,
            command=f"callback:{callback_id}",
            args="",
            allowed=allowed,
            result_status=result_status,
            error_message=error_message if result_status == "error" else ("" if allowed else response),
        )
        if isinstance(response, TelegramResponse):
            return response
        return TelegramResponse(safe_telegram_text(response, token=self.config.bot_token))

    def _page_state(self, state_key: tuple[int, int] | None) -> tuple[str, list[str]]:
        if state_key is None:
            return HOME_PAGE, []
        page, history = self._page_states.get(state_key, (HOME_PAGE, []))
        return page, list(history)

    def _handle_menu_callback(self, telegram_user_id: int, callback_id: str) -> TelegramResponse:
        allowed = self._is_allowed(telegram_user_id)
        response = "admin allowlist missing" if not self.config.admin_user_ids else "unauthorized"
        result_status = "rejected"
        reply_markup: dict[str, Any] | None = None
        if allowed:
            menu_name = MENU_CALLBACKS[callback_id]
            response = menu_text(menu_name)
            reply_markup = menu_reply_markup(menu_name)
            result_status = "ok"
        audit_telegram_command(
            self.config.audit_db_path,
            telegram_user_id=telegram_user_id,
            command=f"callback:{callback_id}",
            args="",
            allowed=allowed,
            result_status=result_status,
            error_message="" if allowed else response,
        )
        return TelegramResponse(safe_telegram_text(response, token=self.config.bot_token), reply_markup=reply_markup)

    def _handle_command(
        self,
        *,
        telegram_user_id: int,
        command: str,
        args: str,
        audit_command: str,
    ) -> TelegramResponse:
        allowed = self._is_allowed(telegram_user_id)
        result_status = "ok"
        error_message = ""
        reply_markup: dict[str, Any] | None = None
        parse_mode: str | None = None
        try:
            if not allowed:
                result_status = "rejected"
                response = "admin allowlist missing" if not self.config.admin_user_ids else "unauthorized"
            elif command in LIVE_COMMANDS:
                result_status = "blocked"
                response = "live trading disabled"
            else:
                dispatched = self._dispatch(command, args)
                response = dispatched.text
                reply_markup = dispatched.reply_markup
                parse_mode = dispatched.parse_mode
        except Exception as exc:
            LOGGER.exception("telegram console command failed command=%s user_id=%s", audit_command, telegram_user_id)
            result_status = "error"
            error_message = str(exc)
            response = "error: command failed safely"
        response = safe_telegram_text(response, token=self.config.bot_token)
        audit_telegram_command(
            self.config.audit_db_path,
            telegram_user_id=telegram_user_id,
            command=audit_command,
            args=args,
            allowed=allowed,
            result_status=result_status,
            error_message=error_message,
        )
        return TelegramResponse(response, reply_markup=reply_markup, parse_mode=parse_mode)

    def _handle_unknown_callback(self, telegram_user_id: int, callback_id: str) -> TelegramResponse:
        allowed = self._is_allowed(telegram_user_id)
        response = "admin allowlist missing" if not self.config.admin_user_ids else "unauthorized"
        result_status = "rejected"
        if allowed:
            response = "unknown action. Try /help"
            result_status = "unknown_callback"
        audit_telegram_command(
            self.config.audit_db_path,
            telegram_user_id=telegram_user_id,
            command=f"callback:{callback_id}",
            args="",
            allowed=allowed,
            result_status=result_status,
            error_message="" if allowed else response,
        )
        return TelegramResponse(safe_telegram_text(response, token=self.config.bot_token), reply_markup=main_menu_reply_markup() if allowed else None)

    def poll_once(self, *, offset: int | None = None, timeout: int = 30) -> int | None:
        self.validate_startup()
        payload = self._telegram_request(
            "getUpdates",
            {
                "timeout": max(1, int(timeout)),
                **({"offset": offset} if offset is not None else {}),
                "allowed_updates": json.dumps(["message", "callback_query"]),
            },
        )
        next_offset = offset
        for update in payload.get("result", []):
            next_offset = int(update["update_id"]) + 1
            callback = update.get("callback_query") or {}
            if callback:
                from_user = callback.get("from") or {}
                message = callback.get("message") or {}
                chat = message.get("chat") or {}
                chat_id = chat.get("id")
                user_id = from_user.get("id")
                callback_query_id = callback.get("id")
                callback_data = str(callback.get("data") or "")
                message_id = message.get("message_id")
                self._answer_callback_query(callback_query_id)
                if chat_id is not None and user_id is not None:
                    callback_id = normalize_callback_id(callback_data)
                    response = self.handle_console_callback(int(chat_id), int(user_id), callback_data)
                    self._edit_console_message(
                        int(chat_id),
                        int(user_id),
                        _optional_int(message_id),
                        response,
                        allow_send_fallback=not self._is_page_callback(callback_id),
                    )
                continue
            message = update.get("message") or {}
            text = str(message.get("text") or "")
            chat = message.get("chat") or {}
            from_user = message.get("from") or {}
            chat_id = chat.get("id")
            user_id = from_user.get("id")
            if chat_id is None or user_id is None or not text.startswith("/"):
                continue
            command, _args = split_command(text)
            if command in {"/start", "/console"}:
                self._page_states[self._active_console_key(int(chat_id), int(user_id))] = (HOME_PAGE, [])
            response = self.handle_text(int(user_id), text)
            self._send_or_edit_console_message(int(chat_id), int(user_id), response)
        return next_offset

    def run_forever(self, *, poll_timeout: int = 30, sleep_seconds: float = 1.0) -> None:
        self.validate_startup()
        LOGGER.info("starting telegram console config=%s", self.config.safe_dict())
        offset: int | None = None
        while True:
            offset = self.poll_once(offset=offset, timeout=poll_timeout)
            time.sleep(max(0.0, sleep_seconds))

    def _dispatch(self, command: str, args: str) -> TelegramResponse:
        if command in {"", "/start", "/console"}:
            return self._page_response(HOME_PAGE)
        if command == "/help":
            return TelegramResponse(help_text(), reply_markup=menu_reply_markup("main"))
        if command == "/status":
            return self._menu_response(
                "Status: read-only; "
                f"paper-only={str(self.config.paper_only).lower()}; "
                f"live={str(self.config.live_enabled and not self.config.paper_only).lower()}"
            )
        if command == "/health":
            return self._menu_response(format_health(self.health_provider()))
        if command == "/signals":
            return self._menu_response(format_signals(self.signals_provider()))
        if command == "/top_wallets":
            rows = self.top_wallets_provider()
            return TelegramResponse(format_top_wallets(rows), reply_markup=_wallet_links_reply_markup(_wallets_from_rows(rows), menu_name="main"))
        if command == "/wallet":
            return self._wallet_response(args)
        if command == "/paper_status":
            return self._menu_response(format_paper_status(self.paper_provider()))
        if command == "/paper_recent":
            paper = self.paper_intelligence_provider()
            return TelegramResponse(paper_recent_report_text(paper), reply_markup=_wallet_links_reply_markup(_wallets_from_rows(paper.get("recent_trades") or []), menu_name="reports"))
        if command == "/paper_pnl":
            return TelegramResponse(paper_pnl_report_text(self.paper_intelligence_provider()), reply_markup=menu_reply_markup("reports"))
        if command == "/paper_positions":
            return TelegramResponse(paper_positions_report_text(self.paper_intelligence_provider()), reply_markup=menu_reply_markup("reports"))
        if command == "/paper_strategies":
            return TelegramResponse(paper_strategies_report_text(self.paper_intelligence_provider()), reply_markup=menu_reply_markup("reports"))
        if command == "/portfolio_analytics":
            return TelegramResponse(portfolio_analytics_report_text(self.paper_intelligence_provider()), reply_markup=menu_reply_markup("reports"))
        if command == "/outcome_validation":
            return TelegramResponse(outcome_validation_report_text(self.paper_intelligence_provider()), reply_markup=menu_reply_markup("reports"))
        if command in {"/paper_opportunities", "/opportunity_rankings"}:
            return self._opportunity_rankings_response()
        if command == "/risk":
            return self._menu_response(format_risk(self.risk_provider()))
        if command == "/report_daily":
            return TelegramResponse(format_daily_intelligence_report(generate_daily_intelligence_report()), reply_markup=menu_reply_markup("reports"))
        if command == "/report_signals":
            return TelegramResponse(signal_summary_report_text(), reply_markup=menu_reply_markup("reports"))
        if command == "/report_wallets":
            return TelegramResponse(
                wallet_summary_report_text(),
                reply_markup=_wallet_link_rows_reply_markup(wallet_summary_link_buttons(), menu_name="reports"),
            )
        if command == "/report_paper":
            return TelegramResponse(paper_performance_report_text(self.paper_intelligence_provider()), reply_markup=menu_reply_markup("reports"))
        return self._menu_response("unknown command. Try /help")

    def _dashboard_response(self) -> TelegramResponse:
        return self._page_response(HOME_PAGE)

    def _page_response(self, page: str) -> TelegramResponse:
        context = self._page_context(include_wallets=page in {"wallets", "wallet_stats"})
        return TelegramResponse(
            render_console_page(page, config=self.config, context=context),
            reply_markup=page_reply_markup(page),
            parse_mode=TELEGRAM_HTML_PARSE_MODE,
        )

    def _page_context(self, *, include_wallets: bool) -> ConsolePageContext:
        return ConsolePageContext(
            now=_as_utc_datetime(self.now_provider()),
            health=self.health_provider(),
            signals=self.signals_provider(),
            paper_health=self.paper_provider(),
            paper=self.paper_intelligence_provider(),
            risk=self.risk_provider(),
            top_wallets=self.top_wallets_provider() if include_wallets else None,
        )

    def _wallet_response(self, args: str) -> TelegramResponse:
        wallet = args.strip().split()[0] if args.strip() else ""
        if not WALLET_RE.match(wallet):
            return TelegramResponse("wallet: provide a valid 0x address")
        summary = self.wallet_provider(wallet.lower())
        if not summary:
            return TelegramResponse(f"Wallet {wallet}: not found", reply_markup=_wallet_links_reply_markup([wallet]))
        text = (
            f"Wallet {short_wallet(wallet)}: "
            f"class={summary.get('classification', 'unknown')} "
            f"score={summary.get('watch_score', 0)} "
            f"confidence={float(summary.get('confidence') or 0):.2f} "
            f"reports={summary.get('report_count', 0)}"
        )
        return TelegramResponse(text, reply_markup=_wallet_links_reply_markup([wallet]))

    def _opportunity_rankings_response(self) -> TelegramResponse:
        from src.analysis.opportunity_feed import get_paper_trading_opportunities
        from src.analysis.opportunity_scoring_v3 import format_opportunity_ranking_text

        rows = get_paper_trading_opportunities(limit=10, portfolio_db_path=self.config.paper_db_path)
        return TelegramResponse(
            format_opportunity_ranking_text(rows, limit=10),
            reply_markup=menu_reply_markup("reports"),
        )

    def _telegram_request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        url = f"{TELEGRAM_API_BASE}/bot{self.config.bot_token}/{method}"
        body = urlencode(params).encode("utf-8")
        request = Request(url, data=body, headers={"Content-Type": "application/x-www-form-urlencoded"})
        with urlopen(request, timeout=35) as response:
            text = response.read().decode("utf-8")
        return json.loads(text) if text else {}

    def _send_message(self, chat_id: int, response: TelegramResponse) -> dict[str, Any]:
        params: dict[str, Any] = {"chat_id": chat_id, "text": response.text}
        if response.parse_mode:
            params["parse_mode"] = response.parse_mode
        if response.reply_markup:
            params["reply_markup"] = json.dumps(response.reply_markup)
        return self._telegram_request("sendMessage", params)

    def _send_or_edit_console_message(
        self,
        chat_id: int,
        telegram_user_id: int,
        response: TelegramResponse,
    ) -> dict[str, Any]:
        key = self._active_console_key(chat_id, telegram_user_id)
        active_message_id = self._active_console_messages.get(key)
        if active_message_id is not None:
            return self._edit_console_message(chat_id, telegram_user_id, active_message_id, response)
        result = self._send_message(chat_id, response)
        self._remember_console_message(chat_id, telegram_user_id, result)
        return result

    def _answer_callback_query(self, callback_query_id: Any) -> None:
        if callback_query_id is None:
            return
        try:
            self._telegram_request("answerCallbackQuery", {"callback_query_id": callback_query_id})
        except Exception:
            LOGGER.warning("telegram answerCallbackQuery failed; continuing")

    def _edit_message_or_send(
        self,
        chat_id: int,
        message_id: int | None,
        response: TelegramResponse,
    ) -> dict[str, Any]:
        result, _message_id = self._edit_message_or_send_with_id(chat_id, message_id, response)
        return result

    def _edit_console_message(
        self,
        chat_id: int,
        telegram_user_id: int,
        message_id: int | None,
        response: TelegramResponse,
        *,
        allow_send_fallback: bool = True,
    ) -> dict[str, Any]:
        key = self._active_console_key(chat_id, telegram_user_id)
        target_message_id = message_id or self._active_console_messages.get(key)
        result, active_message_id = self._edit_message_or_send_with_id(
            chat_id,
            target_message_id,
            response,
            allow_send_fallback=allow_send_fallback,
        )
        if active_message_id is not None:
            self._active_console_messages[key] = active_message_id
        return result

    def _edit_message_or_send_with_id(
        self,
        chat_id: int,
        message_id: int | None,
        response: TelegramResponse,
        *,
        allow_send_fallback: bool = True,
    ) -> tuple[dict[str, Any], int | None]:
        if message_id is not None:
            params: dict[str, Any] = {"chat_id": chat_id, "message_id": message_id, "text": response.text}
            if response.parse_mode:
                params["parse_mode"] = response.parse_mode
            if response.reply_markup:
                params["reply_markup"] = json.dumps(response.reply_markup)
            try:
                return self._telegram_request("editMessageText", params), int(message_id)
            except Exception:
                if not allow_send_fallback:
                    LOGGER.warning("telegram edit failed; refresh will not send a new message")
                    return {"ok": False, "description": "editMessageText failed"}, None
                LOGGER.warning("telegram edit failed; falling back to sendMessage")
        result = self._send_message(chat_id, response)
        return result, _telegram_result_message_id(result)

    def _remember_console_message(self, chat_id: int, telegram_user_id: int, result: dict[str, Any]) -> None:
        message_id = _telegram_result_message_id(result)
        if message_id is not None:
            self._active_console_messages[self._active_console_key(chat_id, telegram_user_id)] = message_id

    def _active_console_key(self, chat_id: int, telegram_user_id: int) -> tuple[int, int]:
        return int(chat_id), int(telegram_user_id)

    def _menu_response(self, text: str) -> TelegramResponse:
        return TelegramResponse(text, reply_markup=menu_reply_markup("main"))


def init_telegram_audit_db(db_path: str | Path = DEFAULT_TELEGRAM_AUDIT_DB) -> None:
    with closing_connection(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS telegram_command_audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp_utc TEXT NOT NULL,
                telegram_user_id INTEGER NOT NULL,
                command TEXT NOT NULL,
                args TEXT NOT NULL DEFAULT '',
                allowed INTEGER NOT NULL,
                result_status TEXT NOT NULL,
                error_message TEXT,
                notification_sent INTEGER,
                notification_type TEXT,
                delivery_status TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_telegram_command_audit_timestamp
                ON telegram_command_audit(timestamp_utc);
            CREATE INDEX IF NOT EXISTS idx_telegram_command_audit_user
                ON telegram_command_audit(telegram_user_id);
            """
        )
        _ensure_audit_column(conn, "notification_sent", "INTEGER")
        _ensure_audit_column(conn, "notification_type", "TEXT")
        _ensure_audit_column(conn, "delivery_status", "TEXT")


def audit_telegram_command(
    db_path: str | Path,
    *,
    telegram_user_id: int,
    command: str,
    args: str,
    allowed: bool,
    result_status: str,
    error_message: str = "",
) -> None:
    init_telegram_audit_db(db_path)
    with closing_connection(db_path) as conn:
        conn.execute(
            """
            INSERT INTO telegram_command_audit (
                timestamp_utc, telegram_user_id, command, args,
                allowed, result_status, error_message
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                utc_now(),
                int(telegram_user_id),
                command,
                args,
                1 if allowed else 0,
                result_status,
                error_message or None,
            ),
        )


def _ensure_audit_column(conn: Any, column: str, definition: str) -> None:
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(telegram_command_audit)").fetchall()}
    if column not in columns:
        conn.execute(f"ALTER TABLE telegram_command_audit ADD COLUMN {column} {definition}")


def parse_admin_user_ids(raw: str) -> frozenset[int]:
    ids: set[int] = set()
    for item in str(raw or "").split(","):
        text = item.strip()
        if not text:
            continue
        try:
            ids.add(int(text))
        except ValueError:
            LOGGER.warning("ignoring invalid telegram admin user id")
    return frozenset(ids)


def split_command(text: str) -> tuple[str, str]:
    parts = str(text or "").strip().split(maxsplit=1)
    if not parts:
        return "", ""
    command = parts[0].split("@", 1)[0].lower()
    return command, parts[1].strip() if len(parts) > 1 else ""


def normalize_callback_id(callback_data: str) -> str:
    callback_id = str(callback_data or "").strip().lower()
    if not re.fullmatch(r"[a-z0-9_]{1,32}", callback_id):
        return "unknown"
    return callback_id


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _telegram_result_message_id(result: dict[str, Any]) -> int | None:
    payload = result.get("result") if isinstance(result, dict) else None
    if not isinstance(payload, dict):
        return None
    return _optional_int(payload.get("message_id"))


def select_telegram_parse_mode(*, use_html: bool = True) -> str | None:
    """Return the safest parse mode for dynamic console content."""
    return TELEGRAM_HTML_PARSE_MODE if use_html else None


def escape_telegram_html(text: Any) -> str:
    return (
        str(text or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def telegram_pre_block(content: str) -> str:
    return f"<pre>{escape_telegram_html(content)}</pre>"


def _table_label_width(rows: list[tuple[str, str]], *, columns: tuple[str, str] | None = None) -> int:
    widths = [len(label) for label, _ in rows]
    if columns:
        widths.append(len(columns[0]))
    return max(widths or [0])


def render_table_section(
    header: str,
    rows: list[tuple[str, str]],
    *,
    columns: tuple[str, str] | None = None,
) -> str:
    if not rows:
        return telegram_pre_block(header)
    label_width = _table_label_width(rows, columns=columns)
    lines = [header]
    if columns:
        lines.append(f"{columns[0].ljust(label_width)}  {columns[1]}")
    for label, value in rows:
        lines.append(f"{label.ljust(label_width)}  {value}")
    return telegram_pre_block("\n".join(lines))


def render_grouped_sections(sections: list[tuple[str, list[tuple[str, str]]]]) -> str:
    blocks = [
        render_table_section(header, rows)
        for header, rows in sections
        if rows
    ]
    return "\n\n".join(blocks)


def render_page_timestamp(now: datetime) -> str:
    return f"Updated:\n{_as_utc_datetime(now).strftime('%H:%M:%S UTC')}"


def render_dashboard_text(
    *,
    config: TelegramConsoleConfig,
    health: dict[str, Any],
    signals: dict[str, Any],
    paper_health: dict[str, Any],
    paper: dict[str, Any],
    risk: dict[str, Any],
    now: datetime,
) -> str:
    now = _as_utc_datetime(now)
    wallet_status = str(health.get("status") or "unknown")
    paper_enabled = bool(config.paper_only or paper.get("paper_only") or paper_health)
    live_enabled = bool(config.live_enabled and not config.paper_only and not risk.get("halted"))
    system_status = "healthy" if wallet_status in {"healthy", "running", "ok"} else wallet_status

    sections = [
        (
            "System Health",
            [
                ("Wallet Autonomy", f"{status_icon(wallet_status)} {service_status_label(wallet_status)}"),
                ("Telegram Console", "🟢 Running"),
                ("Mission Control", "🟢 Online"),
                ("Trader Dashboard", "🟢 Online"),
                ("Health Timer", f"{status_icon(wallet_status)} {_health_timer_label(health, now)}"),
            ],
        ),
        (
            "Trading Mode",
            [
                ("Paper Trading", f"{'🟢' if paper_enabled else '🔴'} {'Enabled' if paper_enabled else 'Disabled'}"),
                ("Live Trading", f"{'🟢' if live_enabled else '🔴'} {'Enabled' if live_enabled else 'Disabled'}"),
            ],
        ),
        (
            "Performance",
            [
                ("Signals Today", format_count_metric(_signals_today(signals, now))),
                ("Paper Trades", format_count_metric(paper.get("trade_count"))),
                ("Validated Families", format_count_metric(_validated_families(paper, signals))),
                ("Validation Accuracy", format_pct_metric(_validation_accuracy(paper, signals))),
                ("Paper Win Rate", format_pct_metric(paper.get("win_rate"))),
                ("Last Cycle", relative_time(_last_cycle_at(health, signals, paper_health), now)),
                ("System Uptime", relative_duration(_parse_datetime(health.get("service_uptime_anchor")), now)),
            ],
        ),
    ]
    header = (
        f"<b>📊 Polylens Mission Control</b>\n"
        f"{status_icon(system_status)} {system_status_label(system_status)}"
    )
    return "\n\n".join([header, render_grouped_sections(sections), render_page_timestamp(now)])


def render_console_page(
    page: str,
    *,
    config: TelegramConsoleConfig,
    context: ConsolePageContext,
) -> str:
    if page == HOME_PAGE:
        return render_dashboard_text(
            config=config,
            health=context.health,
            signals=context.signals,
            paper_health=context.paper_health,
            paper=context.paper,
            risk=context.risk,
            now=context.now,
        )
    if page == "performance":
        return _render_metric_page(
            "📈 Performance",
            [],
            context.now,
            sections=[
                (
                    "Paper Trading",
                    [
                        ("Paper Trades", format_count_metric(context.paper.get("trade_count"))),
                        ("Open Positions", format_count_metric(context.paper.get("open_positions_count"))),
                        ("Closed Trades", format_count_metric(context.paper.get("closed_positions_count"))),
                        ("Win Rate", format_pct_metric(context.paper.get("win_rate"))),
                        ("Top Strategy", _strategy_label(context.paper.get("top_strategy"))),
                    ],
                ),
                (
                    "PnL",
                    [
                        ("Daily PnL", _format_money(context.paper.get("daily_pnl"))),
                        ("7D PnL", _format_money(context.paper.get("pnl_7d"))),
                        ("Total PnL", _format_money(context.paper.get("total_pnl"))),
                    ],
                ),
                (
                    "Validation",
                    [
                        ("Validation Accuracy", format_pct_metric(_validation_accuracy(context.paper, context.signals))),
                        ("Validated Families", format_count_metric(_validated_families(context.paper, context.signals))),
                    ],
                ),
            ],
        )
    if page == "intelligence":
        return _render_metric_page(
            "🧠 Intelligence",
            [],
            context.now,
            sections=[
                (
                    "Signals",
                    [
                        ("Signals Today", format_count_metric(_signals_today(context.signals, context.now))),
                        ("Signals Scored", format_count_metric(context.signals.get("scored_count"))),
                        ("Recommendations", format_count_metric(context.signals.get("recommendation_count"))),
                        ("Last Signal Cycle", relative_time(_last_cycle_at(context.health, context.signals, context.paper_health), context.now)),
                    ],
                ),
                (
                    "Validation",
                    [
                        ("Validation Accuracy", format_pct_metric(_validation_accuracy(context.paper, context.signals))),
                    ],
                ),
            ],
        )
    if page == "wallets":
        wallets = context.top_wallets or []
        sections: list[tuple[str, list[tuple[str, str]]]] = [
            (
                "Wallet Overview",
                [
                    ("Tracked Wallets", format_count_metric(len(wallets))),
                    ("Active Wallets", format_count_metric(_wallet_active_count(wallets))),
                    ("Wallet Service", f"{status_icon(context.health.get('status'))} {service_status_label(context.health.get('status'))}"),
                    ("Service Uptime", relative_duration(_parse_datetime(context.health.get("service_uptime_anchor")), context.now)),
                ],
            ),
        ]
        lifecycle_rows = _wallet_lifecycle_rows(wallets)
        if lifecycle_rows:
            sections.append(("Lifecycle", lifecycle_rows))
        top_rows = _wallet_metric_rows(wallets)
        if top_rows and top_rows != [("Top Wallets", "None")]:
            sections.append(("Top Wallets", top_rows))
        return _render_metric_page("👛 Wallets", [], context.now, sections=sections)
    if page == "reports":
        return _render_metric_page(
            "📊 Reports",
            [],
            context.now,
            sections=[
                (
                    "Daily Summary",
                    [
                        ("Daily PnL", _format_money(context.paper.get("daily_pnl"))),
                        ("Signals Today", format_count_metric(_signals_today(context.signals, context.now))),
                        ("Paper Trades", format_count_metric(context.paper.get("trade_count"))),
                    ],
                ),
                (
                    "Weekly Summary",
                    [
                        ("7D PnL", _format_money(context.paper.get("pnl_7d"))),
                        ("Win Rate", format_pct_metric(context.paper.get("win_rate"))),
                        ("Validated Families", format_count_metric(_validated_families(context.paper, context.signals))),
                    ],
                ),
                (
                    "Monthly Summary",
                    [
                        ("Total PnL", _format_money(context.paper.get("total_pnl"))),
                        ("Closed Trades", format_count_metric(context.paper.get("closed_positions_count"))),
                        ("Validation Accuracy", format_pct_metric(_validation_accuracy(context.paper, context.signals))),
                    ],
                ),
            ],
        )
    if page == "system":
        return _render_metric_page(
            "⚙️ System",
            [],
            context.now,
            sections=_system_page_sections(context),
        )
    if page == "paper_trades":
        return _render_detail_page(
            "📈 Paper Trades",
            _paper_trade_details(context.paper),
            context.now,
        )
    if page == "signals_today":
        return _render_detail_page(
            "🧠 Signals Today",
            [
                f"Generated: {format_count_metric(_signals_today(context.signals, context.now))}",
                f"Scored: {format_count_metric(context.signals.get('scored_count'))}",
                f"Recommendations: {format_count_metric(context.signals.get('recommendation_count'))}",
                f"Last Cycle: {relative_time(_last_cycle_at(context.health, context.signals, context.paper_health), context.now)}",
            ],
            context.now,
        )
    if page == "wallet_stats":
        return _render_detail_page(
            "👛 Wallet Stats",
            _wallet_detail_rows(context.top_wallets or []),
            context.now,
            section_header="Top Wallets",
        )
    if page == "validation_accuracy":
        return _render_detail_page(
            "🧠 Validation Accuracy",
            [
                f"Accuracy: {format_pct_metric(_validation_accuracy(context.paper, context.signals))}",
                f"Validated Families: {format_count_metric(_validated_families(context.paper, context.signals))}",
                f"Paper Win Rate: {format_pct_metric(context.paper.get('win_rate'))}",
            ],
            context.now,
        )
    return _render_metric_page("Polylens", [("Status", "Unknown page")], context.now)


def page_reply_markup(page: str) -> dict[str, Any]:
    if page == HOME_PAGE:
        return _menu_markup(
            [
                [
                    {"text": "📈 Performance", "callback_data": "nav_performance"},
                    {"text": "🧠 Intelligence", "callback_data": "nav_intelligence"},
                ],
                [
                    {"text": "👛 Wallets", "callback_data": "nav_wallets"},
                    {"text": "📊 Reports", "callback_data": "nav_reports"},
                ],
                [
                    {"text": "⚙️ System", "callback_data": "nav_system"},
                    {"text": "🔄 Refresh", "callback_data": "refresh_page"},
                ],
            ]
        )
    page_rows = {
        "performance": [
            [{"text": "Paper Trades", "callback_data": "drill_paper_trades"}],
            [{"text": "Validation", "callback_data": "drill_validation"}],
        ],
        "intelligence": [
            [{"text": "Signals Today", "callback_data": "drill_signals_today"}],
            [{"text": "Validation", "callback_data": "drill_validation"}],
        ],
        "wallets": [[{"text": "Wallet Stats", "callback_data": "drill_wallet_stats"}]],
        "reports": [
            [{"text": "Paper Trades", "callback_data": "drill_paper_trades"}],
            [{"text": "Signals Today", "callback_data": "drill_signals_today"}],
        ],
        "system": [[{"text": "Wallet Stats", "callback_data": "drill_wallet_stats"}]],
    }
    return _menu_markup(
        [
            *page_rows.get(page, []),
            [
                {"text": "🏠 Home", "callback_data": "nav_home"},
                {"text": "⬅️ Back", "callback_data": "nav_back"},
            ],
            [{"text": "🔄 Refresh", "callback_data": "refresh_page"}],
        ]
    )


def _render_metric_page(
    title: str,
    rows: list[tuple[str, str]],
    now: datetime,
    *,
    sections: list[tuple[str, list[tuple[str, str]]]] | None = None,
) -> str:
    grouped = sections if sections is not None else [(title.split(" ", 1)[-1], rows)]
    body = render_grouped_sections(grouped)
    return "\n\n".join([f"<b>{escape_telegram_html(title)}</b>", body, render_page_timestamp(now)])


def _render_detail_page(
    title: str,
    details: list[str],
    now: datetime,
    *,
    section_header: str | None = None,
) -> str:
    header = section_header or "Details"
    body = telegram_pre_block("\n".join([header, *details]))
    return "\n\n".join([f"<b>{escape_telegram_html(title)}</b>", body, render_page_timestamp(now)])


def _format_money(value: Any) -> str:
    try:
        amount = float(value or 0.0)
    except (TypeError, ValueError):
        amount = 0.0
    return f"{'+' if amount > 0 else ''}${amount:.2f}"


def _strategy_label(strategy: Any) -> str:
    if not isinstance(strategy, dict):
        return "N/A"
    return str(strategy.get("strategy") or "N/A")


def _wallet_metric_rows(wallets: list[Any]) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    for index, item in enumerate(wallets[:3], start=1):
        row = item.to_dict() if hasattr(item, "to_dict") else item
        if not isinstance(row, dict):
            continue
        wallet = short_wallet(row.get("wallet") or row.get("address"))
        score = row.get("watch_score", 0)
        rows.append((f"#{index}", f"{wallet} score={score}"))
    return rows or [("Top Wallets", "None")]


def _wallet_active_count(wallets: list[Any]) -> int:
    active = 0
    for item in wallets:
        row = item.to_dict() if hasattr(item, "to_dict") else item
        if not isinstance(row, dict):
            continue
        state = str(row.get("lifecycle_state") or row.get("state") or "active").lower()
        if state not in {"retired", "inactive"}:
            active += 1
    return active or len(wallets)


def _wallet_lifecycle_counts(wallets: list[Any]) -> dict[str, int]:
    counts = {"promoted": 0, "probation": 0, "retired": 0}
    for item in wallets:
        row = item.to_dict() if hasattr(item, "to_dict") else item
        if not isinstance(row, dict):
            continue
        state = str(row.get("lifecycle_state") or row.get("state") or "").lower()
        if state in counts:
            counts[state] += 1
    return counts


def _wallet_lifecycle_rows(wallets: list[Any]) -> list[tuple[str, str]]:
    counts = _wallet_lifecycle_counts(wallets)
    rows: list[tuple[str, str]] = []
    if counts["promoted"]:
        rows.append(("Promoted", format_count_metric(counts["promoted"])))
    if counts["probation"]:
        rows.append(("Probation", format_count_metric(counts["probation"])))
    if counts["retired"]:
        rows.append(("Retired", format_count_metric(counts["retired"])))
    return rows


def _wallet_detail_rows(wallets: list[Any]) -> list[str]:
    if not wallets:
        return ["No wallet metrics available."]
    lines = [f"{'#':<3} {'Wallet':<18} {'Score':<6} Class"]
    for index, item in enumerate(wallets[:5], start=1):
        row = item.to_dict() if hasattr(item, "to_dict") else item
        if not isinstance(row, dict):
            continue
        wallet = short_wallet(row.get("wallet") or row.get("address"))
        score = row.get("watch_score", 0)
        classification = row.get("classification", "unknown")
        lines.append(f"{index:<3} {wallet:<18} {score:<6} {classification}")
    return lines or ["No wallet metrics available."]


def _system_resource_rows(health: dict[str, Any], now: datetime) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    disk = health.get("disk_usage_pct")
    if disk is None:
        disk = health.get("disk_free_pct")
    if disk is not None:
        rows.append(("Disk Usage", format_pct_metric(disk)))
    memory = health.get("memory_usage_pct")
    if memory is None:
        memory = health.get("memory_used_pct")
    if memory is not None:
        rows.append(("Memory Usage", format_pct_metric(memory)))
    uptime_anchor = health.get("service_uptime_anchor")
    if uptime_anchor:
        rows.append(("System Uptime", relative_duration(_parse_datetime(uptime_anchor), now)))
    return rows


def _system_page_sections(context: ConsolePageContext) -> list[tuple[str, list[tuple[str, str]]]]:
    sections: list[tuple[str, list[tuple[str, str]]]] = [
        (
            "Services",
            [
                ("Wallet Autonomy", f"{status_icon(context.health.get('status'))} {service_status_label(context.health.get('status'))}"),
                ("Telegram Console", "🟢 Running"),
                ("Paper Trading", f"{status_icon(context.paper_health.get('status'))} {service_status_label(context.paper_health.get('status'))}"),
                ("Live Trading", "🔴 Disabled"),
            ],
        ),
        (
            "Health",
            [
                ("Health Timer", f"{status_icon(context.health.get('status'))} {_health_timer_label(context.health, context.now)}"),
                ("Last Cycle", relative_time(_last_cycle_at(context.health, context.signals, context.paper_health), context.now)),
            ],
        ),
    ]
    resources = _system_resource_rows(context.health, context.now)
    if resources:
        sections.append(("Resources", resources))
    return sections


def _paper_trade_details(paper: dict[str, Any]) -> list[str]:
    rows = [
        f"Total: {format_count_metric(paper.get('trade_count'))}",
        f"Open: {format_count_metric(paper.get('open_positions_count'))}",
        f"Closed: {format_count_metric(paper.get('closed_positions_count'))}",
    ]
    for trade in (paper.get("recent_trades") or [])[:3]:
        if isinstance(trade, dict):
            rows.append(
                f"{trade.get('strategy', 'paper')} | {trade.get('status', 'unknown')} | {_format_money(trade.get('pnl'))}"
            )
    return rows


def status_icon(status: Any) -> str:
    text = str(status or "").strip().lower()
    if text in {"healthy", "running", "ok", "online", "enabled", "success"}:
        return "🟢"
    if text in {"degraded", "idle", "warn", "warning", "stale"}:
        return "🟡"
    return "🔴"


def system_status_label(status: Any) -> str:
    text = str(status or "").strip().lower()
    if text in {"healthy", "running", "ok", "online"}:
        return "System Healthy"
    if text in {"degraded", "idle", "warn", "warning", "stale"}:
        return "System Degraded"
    return "System Unhealthy"


def service_status_label(status: Any) -> str:
    text = str(status or "").strip().lower()
    if text in {"healthy", "running", "ok", "success"}:
        return "Running"
    if text in {"idle", "degraded", "warn", "warning", "stale"}:
        return "Degraded"
    return "Offline"


def format_count_metric(value: Any) -> str:
    try:
        return f"{int(float(value or 0)):,}"
    except (TypeError, ValueError):
        return "0"


def format_pct_metric(value: Any) -> str:
    try:
        number = float(value or 0.0)
    except (TypeError, ValueError):
        number = 0.0
    if abs(number) <= 1.0:
        number *= 100.0
    return f"{number:.1f}%"


def relative_time(value: Any, now: datetime) -> str:
    parsed = _parse_datetime(value)
    if parsed is None:
        return "N/A"
    seconds = max(0, int((_as_utc_datetime(now) - parsed).total_seconds()))
    if seconds < 60:
        return f"{seconds}s ago"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h ago"
    return f"{hours // 24}d ago"


def relative_duration(value: Any, now: datetime) -> str:
    parsed = _parse_datetime(value)
    if parsed is None:
        return "N/A"
    seconds = max(0, int((_as_utc_datetime(now) - parsed).total_seconds()))
    days = seconds // 86400
    hours = (seconds % 86400) // 3600
    minutes = (seconds % 3600) // 60
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def _health_timer_label(health: dict[str, Any], now: datetime) -> str:
    checked_at = _parse_datetime(health.get("checked_at")) or _last_cycle_at(health, {}, {})
    if checked_at is None:
        return "N/A"
    return relative_time(checked_at, now)


def _signals_today(signals: dict[str, Any], now: datetime) -> int:
    explicit = signals.get("signals_today")
    if explicit is not None:
        return int(float(explicit or 0))
    last_cycle = signals.get("last_cycle") if isinstance(signals.get("last_cycle"), dict) else {}
    cycle_at = _parse_datetime(last_cycle.get("cycle_timestamp"))
    if cycle_at and cycle_at.date() == _as_utc_datetime(now).date():
        return int(last_cycle.get("signals_generated") or 0)
    generated = signals.get("signals_generated_today")
    if generated is not None:
        return int(float(generated or 0))
    return int(signals.get("signal_count") or 0)


def _validated_families(paper: dict[str, Any], signals: dict[str, Any]) -> int:
    explicit = signals.get("validated_families") or signals.get("proven_strategy_families")
    if explicit is not None:
        return int(float(explicit or 0))
    breakdown = paper.get("strategy_breakdown") if isinstance(paper.get("strategy_breakdown"), dict) else {}
    return sum(
        1
        for row in breakdown.values()
        if int(row.get("closed_positions") or 0) > 0 and float(row.get("win_rate") or 0.0) > 0.0
    )


def _validation_accuracy(paper: dict[str, Any], signals: dict[str, Any]) -> Any:
    for key in ("validation_accuracy", "accuracy", "success_rate"):
        if signals.get(key) is not None:
            return signals.get(key)
    return paper.get("win_rate")


def _last_cycle_at(health: dict[str, Any], signals: dict[str, Any], paper_health: dict[str, Any]) -> datetime | None:
    candidates: list[datetime] = []
    for cycle in health.get("cycles") or []:
        parsed = _parse_datetime(cycle.get("last_run_at") if isinstance(cycle, dict) else None)
        if parsed:
            candidates.append(parsed)
    last_cycle = signals.get("last_cycle") if isinstance(signals.get("last_cycle"), dict) else {}
    for value in (
        last_cycle.get("cycle_timestamp"),
        paper_health.get("last_run"),
    ):
        parsed = _parse_datetime(value)
        if parsed:
            candidates.append(parsed)
    return max(candidates) if candidates else None


def _parse_datetime(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return _as_utc_datetime(value)
    text = str(value).strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return _as_utc_datetime(parsed)


def _as_utc_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def help_text() -> str:
    return "Polylens Telegram console\nCommands:\n" + "\n".join(COMMANDS)


def menu_text(menu_name: str) -> str:
    titles = {
        "main": "Polylens Control Console",
        "intelligence": "Intelligence",
        "wallets": "Wallets",
        "signals": "Signals",
        "system": "System",
        "reports": "Reports",
    }
    subtitles = {
        "main": "Choose a category.",
        "intelligence": "Read-only intelligence tools.",
        "wallets": "Wallet intelligence and lookup tools.",
        "signals": "Read-only signal engine views.",
        "system": "Safe service, paper, and risk status.",
        "reports": "Concise reports and help.",
    }
    title = titles.get(menu_name, titles["main"])
    subtitle = subtitles.get(menu_name, subtitles["main"])
    return f"{title}\n{subtitle}"


def menu_reply_markup(menu_name: str = "main") -> dict[str, Any]:
    if menu_name == "main":
        return page_reply_markup(HOME_PAGE)
    if menu_name == "intelligence":
        return _menu_markup(
            [
                [{"text": "Health", "callback_data": "health"}],
                [{"text": "Risk", "callback_data": "risk"}],
                [_back_button()],
            ]
        )
    if menu_name == "wallets":
        return _menu_markup(
            [
                [{"text": "Top Wallets", "callback_data": "top_wallets"}],
                [{"text": "Wallet Help", "callback_data": "help"}],
                [_back_button()],
            ]
        )
    if menu_name == "signals":
        return _menu_markup(
            [
                [{"text": "Signals", "callback_data": "signals"}],
                [_back_button()],
            ]
        )
    if menu_name == "system":
        return _menu_markup(
            [
                [
                    {"text": "Status", "callback_data": "status"},
                    {"text": "Health", "callback_data": "health"},
                ],
                [
                    {"text": "Paper Status", "callback_data": "paper_status"},
                    {"text": "Risk", "callback_data": "risk"},
                ],
                [_back_button()],
            ]
        )
    if menu_name == "reports":
        return _menu_markup(
            [
                [{"text": "Daily Brief", "callback_data": "report_daily"}],
                [{"text": "Signal Summary", "callback_data": "report_signals"}],
                [{"text": "Wallet Summary", "callback_data": "report_wallets"}],
                [{"text": "Paper Performance", "callback_data": "report_paper"}],
                [{"text": "Recent Paper Trades", "callback_data": "paper_recent"}],
                [{"text": "Paper PnL", "callback_data": "paper_pnl"}],
                [{"text": "Open Positions", "callback_data": "paper_positions"}],
                [{"text": "Paper Strategies", "callback_data": "paper_strategies"}],
                [{"text": "Outcome Validation", "callback_data": "outcome_validation"}],
                [_back_button()],
            ]
        )
    return page_reply_markup(HOME_PAGE)


def main_menu_reply_markup() -> dict[str, Any]:
    return menu_reply_markup("main")


def _wallet_links_reply_markup(wallets: list[Any], *, menu_name: str | None = None) -> dict[str, Any] | None:
    return _wallet_link_rows_reply_markup(polymarket_analytics_wallet_buttons(wallets), menu_name=menu_name)


def _wallet_link_rows_reply_markup(rows: list[list[dict[str, str]]], *, menu_name: str | None = None) -> dict[str, Any] | None:
    menu_rows = menu_reply_markup(menu_name)["inline_keyboard"] if menu_name else []
    combined = [*rows, *menu_rows]
    return _menu_markup(combined) if combined else None


def _wallets_from_rows(rows: Any) -> list[str]:
    wallets: list[str] = []
    for row in rows or []:
        item = row.to_dict() if hasattr(row, "to_dict") else row
        if not isinstance(item, dict):
            continue
        wallet = str(item.get("wallet") or item.get("address") or item.get("trader_wallet") or "").strip()
        if wallet:
            wallets.append(wallet)
    return wallets


def _menu_markup(rows: list[list[dict[str, str]]]) -> dict[str, Any]:
    return {"inline_keyboard": rows}


def _back_button() -> dict[str, str]:
    return {"text": "⬅️ Back", "callback_data": "nav_back"}


def format_health(payload: dict[str, Any]) -> str:
    stale = payload.get("stale_cycles") or []
    failures = payload.get("failures") or []
    return (
        f"Health: {payload.get('status', 'unknown')}; "
        f"success_rate={float(payload.get('success_rate') or 0):.2f}; "
        f"stale={len(stale)}; failures={len(failures)}"
    )


def format_signals(payload: dict[str, Any]) -> str:
    return (
        f"Signals: {payload.get('status', 'unknown')}; "
        f"signals={int(payload.get('signal_count') or 0)}; "
        f"scored={int(payload.get('scored_count') or 0)}; "
        f"recommendations={int(payload.get('recommendation_count') or 0)}"
    )


def format_top_wallets(rows: list[Any]) -> str:
    if not rows:
        return "Top wallets: none"
    lines = ["Top wallets:"]
    for index, row in enumerate(rows[:5], start=1):
        item = row.to_dict() if hasattr(row, "to_dict") else dict(row)
        lines.append(
            f"{index}. {short_wallet(item.get('wallet', ''))} "
            f"score={item.get('watch_score', 0)} "
            f"class={item.get('classification', 'unknown')}"
        )
    return "\n".join(lines)


def format_paper_status(payload: dict[str, Any]) -> str:
    return (
        f"Paper: {payload.get('status', 'unknown')}; "
        f"runs_24h={int(payload.get('runs_24h') or 0)}; "
        f"open={int(payload.get('positions_open') or 0)}; "
        f"equity={float(payload.get('equity') or 0):.2f}"
    )


def format_risk(payload: dict[str, Any]) -> str:
    halted = bool(payload.get("halted"))
    active = payload.get("active_halts") or []
    return f"Risk: halted={str(halted).lower()}; active_halts={len(active)}; live trading disabled"


def safe_telegram_text(text: str, *, token: str = "") -> str:
    safe = str(text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if token:
        safe = safe.replace(token, "[redacted]")
    if len(safe) > MAX_TELEGRAM_TEXT:
        safe = safe[: MAX_TELEGRAM_TEXT - 20].rstrip() + "\n[truncated]"
    return safe or "ok"


def short_wallet(wallet: Any) -> str:
    text = str(wallet or "")
    if len(text) <= 12:
        return text
    return f"{text[:6]}...{text[-4:]}"


def redact_token(token: str) -> str:
    return "redacted" if token else ""


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _env_bool(name: str, *, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def run_telegram_console(
    *,
    once: bool = False,
    poll_timeout: int = 30,
    audit_db_path: str | None = None,
) -> TelegramConsole:
    config = TelegramConsoleConfig.from_env(audit_db_path=audit_db_path)
    console = TelegramConsole(config)
    if once:
        console.poll_once(timeout=poll_timeout)
    else:
        console.run_forever(poll_timeout=poll_timeout)
    return console
from dataclasses import field as _v4_field
from time import perf_counter as _v4_perf_counter


V4_FAVORITE_TOGGLE_CALLBACK = "favorite_toggle"
V4_FAVORITE_PAGE_CALLBACKS = {
    "fav_paper_trades": "paper_trades",
    "fav_wallet_stats": "wallet_stats",
    "fav_performance": "performance",
    "fav_intelligence": "intelligence",
    "fav_wallets": "wallets",
    "fav_reports": "reports",
    "fav_system": "system",
}
V4_CONTEXT_CALLBACKS = {
    "quick_paper_trades": "paper_trades",
    "quick_win_rate": "win_rate",
    "quick_validation": "validation_accuracy",
    "quick_signals_today": "signals_today",
    "quick_top_wallet": "top_wallet",
    "quick_new_wallets": "new_wallets",
    "quick_wallet_stats": "wallet_stats",
    "quick_report_daily": "daily_report",
    "quick_report_weekly": "weekly_report",
    "quick_report_monthly": "monthly_report",
    "quick_disk": "disk",
    "quick_memory": "memory",
    "quick_services": "services",
    "quick_opportunity_rankings": "opportunity_rankings",
}
V4_PAGE_TITLES = {
    "home": "🏠 Home",
    "performance": "📈 Performance",
    "intelligence": "🧠 Intelligence",
    "wallets": "👛 Wallets",
    "reports": "📊 Reports",
    "system": "⚙️ System",
    "paper_trades": "📄 Paper Trades",
    "signals_today": "Signals Today",
    "wallet_stats": "Wallet Stats",
    "validation_accuracy": "🎯 Validation",
    "win_rate": "📈 Win Rate",
    "top_wallet": "⭐ Top Wallet",
    "new_wallets": "🆕 New Wallets",
    "daily_report": "📅 Daily",
    "weekly_report": "📆 Weekly",
    "monthly_report": "📈 Monthly",
    "disk": "💾 Disk",
    "memory": "🧠 Memory",
    "services": "⚙️ Services",
    "opportunity_rankings": "🏆 Opportunity Rankings",
    "trades_log": "📜 Trades Log",
}
V4_WALLET_PAGES = {"wallets", "wallet_stats", "top_wallet", "new_wallets"}


@dataclass
class ConsoleNavigationState:
    current_page: str = HOME_PAGE
    history: list[str] = _v4_field(default_factory=list)
    favorites: list[str] = _v4_field(default_factory=list)
    recent_pages: list[str] = _v4_field(default_factory=list)


_v3_console_init = TelegramConsole.__init__
_v3_handle_text = TelegramConsole.handle_text
_v3_handle_callback = TelegramConsole.handle_callback
_v3_handle_console_callback = TelegramConsole.handle_console_callback
_v3_page_context = TelegramConsole._page_context
_v3_render_dashboard_text = render_dashboard_text
_v3_render_console_page = render_console_page
_v3_render_metric_page = _render_metric_page
_v3_render_detail_page = _render_detail_page


def render_timing_ms(render: Callable[[], Any]) -> tuple[Any, float]:
    """Render a read-only page and return its elapsed wall-clock time in milliseconds."""
    started = _v4_perf_counter()
    result = render()
    return result, (_v4_perf_counter() - started) * 1000


def _v4_console_init(self: TelegramConsole, *args: Any, **kwargs: Any) -> None:
    _v3_console_init(self, *args, **kwargs)
    self._v4_navigation_states: dict[int, ConsoleNavigationState] = {}
    self._v4_context_cache: dict[bool, tuple[float, ConsolePageContext]] = {}


def _v4_state(self: TelegramConsole, telegram_user_id: int) -> ConsoleNavigationState:
    return self._v4_navigation_states.setdefault(int(telegram_user_id), ConsoleNavigationState())


def _v4_is_page_callback(self: TelegramConsole, callback_id: str) -> bool:
    return (
        callback_id in PAGE_NAV_CALLBACKS
        or callback_id in DRILLDOWN_CALLBACKS
        or callback_id in REFRESH_CALLBACKS
        or callback_id in BACK_CALLBACKS
        or callback_id == V4_FAVORITE_TOGGLE_CALLBACK
        or callback_id in V4_FAVORITE_PAGE_CALLBACKS
        or callback_id in V4_CONTEXT_CALLBACKS
    )


def _v4_remember_recent(state: ConsoleNavigationState, page: str) -> None:
    if page == HOME_PAGE:
        return
    state.recent_pages = [page, *[item for item in state.recent_pages if item != page]][:5]


def _v4_breadcrumb(page: str, history: list[str]) -> str:
    path = [item for item in [*history, page] if item]
    if not path or path[0] != HOME_PAGE:
        path.insert(0, HOME_PAGE)
    return " › ".join(V4_PAGE_TITLES.get(item, item.replace("_", " ").title()) for item in path)


def _v4_chrome(
    *,
    page: str,
    history: list[str],
    favorites: list[str],
    recent_pages: list[str],
) -> list[str]:
    lines = [_v4_breadcrumb(page, history)]
    if favorites:
        lines.extend(["", "⭐ Favorites", *[f"• {V4_PAGE_TITLES.get(item, item.replace('_', ' ').title())}" for item in favorites]])
    visible_recent = [item for item in recent_pages if item != page][:5]
    if visible_recent:
        lines.extend(["", "🕒 Recent", *[f"• {V4_PAGE_TITLES.get(item, item.replace('_', ' ').title())}" for item in visible_recent]])
    return lines


def _v4_wrap_page(text: str, *, page: str, history: list[str], favorites: list[str], recent_pages: list[str]) -> str:
    return "\n".join([*_v4_chrome(page=page, history=history, favorites=favorites, recent_pages=recent_pages), "", text])


def _v4_detail_rows(page: str, context: ConsolePageContext) -> list[str]:
    if page == "win_rate":
        return [
            f"Paper Win Rate: {format_pct_metric(context.paper.get('win_rate'))}",
            f"Closed Paper Trades: {format_count_metric(context.paper.get('closed_positions_count'))}",
            f"Validation Accuracy: {format_pct_metric(_validation_accuracy(context.paper, context.signals))}",
        ]
    if page == "top_wallet":
        details = _wallet_detail_rows(context.top_wallets or [])
        return details[:1] or ["No tracked wallet is available."]
    if page == "new_wallets":
        return [
            f"Tracked Wallets: {format_count_metric(len(context.top_wallets or []))}",
            "New-wallet discovery is read-only.",
            *_wallet_detail_rows(context.top_wallets or [])[:3],
        ]
    if page in {"daily_report", "weekly_report", "monthly_report"}:
        period = V4_PAGE_TITLES[page]
        return [
            f"{period} PnL: {_format_money(context.paper.get('daily_pnl') if page == 'daily_report' else context.paper.get('pnl_7d'))}",
            f"Signals Today: {format_count_metric(_signals_today(context.signals, context.now))}",
            f"Paper Trades: {format_count_metric(context.paper.get('trade_count'))}",
            "Report data is read-only.",
        ]
    if page == "disk":
        return ["Disk telemetry: not exposed by the read-only health provider.", "Service health remains read-only."]
    if page == "memory":
        return ["Memory telemetry: not exposed by the read-only health provider.", "Service health remains read-only."]
    if page == "services":
        return [
            f"Wallet Autonomy: {status_icon(context.health.get('status'))} {service_status_label(context.health.get('status'))}",
            f"Paper Trading: {status_icon(context.paper_health.get('status'))} {service_status_label(context.paper_health.get('status'))}",
            "Live Trading: 🔴 Disabled",
        ]
    if page == "opportunity_rankings":
        from src.analysis.opportunity_feed import get_paper_trading_opportunities
        from src.analysis.opportunity_scoring_v3 import format_opportunity_ranking_text
        from src.analysis.paper_trading_engine import DEFAULT_PAPER_TRADING_DB

        rows = get_paper_trading_opportunities(limit=10, portfolio_db_path=DEFAULT_PAPER_TRADING_DB)
        return format_opportunity_ranking_text(rows, limit=10).splitlines()
    return ["Read-only detail unavailable."]


def render_console_page(
    page: str,
    *,
    config: TelegramConsoleConfig,
    context: ConsolePageContext,
    history: list[str] | None = None,
    favorites: list[str] | None = None,
    recent_pages: list[str] | None = None,
) -> str:
    history = list(history or [])
    favorites = list(favorites or [])
    recent_pages = list(recent_pages or [])
    if page == HOME_PAGE:
        body = _v3_render_dashboard_text(
            config=config,
            health=context.health,
            signals=context.signals,
            paper_health=context.paper_health,
            paper=context.paper,
            risk=context.risk,
            now=context.now,
        )
    elif page in {"win_rate", "top_wallet", "new_wallets", "daily_report", "weekly_report", "monthly_report", "disk", "memory", "services"}:
        body = _v3_render_detail_page(V4_PAGE_TITLES[page], _v4_detail_rows(page, context), context.now)
    else:
        body = _v3_render_console_page(page, config=config, context=context)
    return _v4_wrap_page(body, page=page, history=history, favorites=favorites, recent_pages=recent_pages)


def _v4_favorite_buttons(favorites: list[str]) -> list[list[dict[str, str]]]:
    rows: list[list[dict[str, str]]] = []
    for page in favorites:
        callback_id = next((key for key, value in V4_FAVORITE_PAGE_CALLBACKS.items() if value == page), None)
        if callback_id:
            rows.append([{"text": f"⭐ {V4_PAGE_TITLES.get(page, page)}", "callback_data": callback_id}])
    return rows


def page_reply_markup(page: str, state: ConsoleNavigationState | None = None) -> dict[str, Any]:
    state = state or ConsoleNavigationState(current_page=page)
    home_rows = [
        [
            {"text": "📈 Performance", "callback_data": "nav_performance"},
            {"text": "🧠 Intelligence", "callback_data": "nav_intelligence"},
        ],
        [
            {"text": "👛 Wallets", "callback_data": "nav_wallets"},
            {"text": "📊 Reports", "callback_data": "nav_reports"},
        ],
        [{"text": "⚙️ System", "callback_data": "nav_system"}],
    ]
    context_rows = {
        "performance": [[
            {"text": "📄 Paper Trades", "callback_data": "quick_paper_trades"},
            {"text": "📈 Win Rate", "callback_data": "quick_win_rate"},
        ], [{"text": "🎯 Validation", "callback_data": "quick_validation"}]],
        "intelligence": [[
            {"text": "Signals Today", "callback_data": "quick_signals_today"},
            {"text": "🎯 Validation", "callback_data": "quick_validation"},
        ]],
        "wallets": [[
            {"text": "⭐ Top Wallet", "callback_data": "quick_top_wallet"},
            {"text": "🆕 New Wallets", "callback_data": "quick_new_wallets"},
        ], [{"text": "📊 Wallet Stats", "callback_data": "quick_wallet_stats"}]],
        "reports": [[
            {"text": "📅 Daily", "callback_data": "quick_report_daily"},
            {"text": "📆 Weekly", "callback_data": "quick_report_weekly"},
        ], [{"text": "📈 Monthly", "callback_data": "quick_report_monthly"}]],
        "system": [[
            {"text": "💾 Disk", "callback_data": "quick_disk"},
            {"text": "🧠 Memory", "callback_data": "quick_memory"},
        ], [{"text": "⚙️ Services", "callback_data": "quick_services"}]],
    }
    rows = [*(home_rows if page == HOME_PAGE else context_rows.get(page, []))]
    rows.extend(_v4_favorite_buttons(state.favorites))
    label = "★ Unpin Page" if page in state.favorites else "⭐ Favorite Page"
    rows.append([{"text": label, "callback_data": V4_FAVORITE_TOGGLE_CALLBACK}])
    bottom = [{"text": "🏠 Home", "callback_data": "nav_home"}]
    if page != HOME_PAGE:
        bottom.append({"text": "◀ Back", "callback_data": "nav_back"})
    bottom.append({"text": "🔄 Refresh", "callback_data": "refresh_page"})
    rows.append(bottom)
    return _menu_markup(rows)


def _v4_page_context(self: TelegramConsole, *, include_wallets: bool) -> ConsolePageContext:
    now = _as_utc_datetime(self.now_provider())
    cached = self._v4_context_cache.get(include_wallets)
    if cached and cached[0] > _v4_perf_counter():
        snapshot = cached[1]
        return ConsolePageContext(
            now=now,
            health=snapshot.health,
            signals=snapshot.signals,
            paper_health=snapshot.paper_health,
            paper=snapshot.paper,
            risk=snapshot.risk,
            top_wallets=snapshot.top_wallets,
        )
    snapshot = _v3_page_context(self, include_wallets=include_wallets)
    self._v4_context_cache[include_wallets] = (_v4_perf_counter() + 7.5, snapshot)
    return snapshot


def _v4_page_response(self: TelegramConsole, page: str, state: ConsoleNavigationState | None = None) -> TelegramResponse:
    state = state or ConsoleNavigationState(current_page=page)
    context = self._page_context(include_wallets=page in V4_WALLET_PAGES)
    return TelegramResponse(
        render_console_page(
            page,
            config=self.config,
            context=context,
            history=state.history,
            favorites=state.favorites,
            recent_pages=state.recent_pages,
        ),
        reply_markup=page_reply_markup(page, state),
        parse_mode=TELEGRAM_HTML_PARSE_MODE,
    )


def _v4_handle_page_callback(self: TelegramConsole, telegram_user_id: int, callback_id: str) -> TelegramResponse:
    allowed = self._is_allowed(telegram_user_id)
    response: TelegramResponse | str = "admin allowlist missing" if not self.config.admin_user_ids else "unauthorized"
    result_status = "rejected"
    error_message = ""
    try:
        if allowed:
            state = _v4_state(self, telegram_user_id)
            page = state.current_page
            if callback_id in REFRESH_CALLBACKS:
                target = page
            elif callback_id in BACK_CALLBACKS:
                _v4_remember_recent(state, page)
                target = state.history.pop() if state.history else HOME_PAGE
            elif callback_id == V4_FAVORITE_TOGGLE_CALLBACK:
                if page in state.favorites:
                    state.favorites.remove(page)
                elif page != HOME_PAGE:
                    state.favorites.append(page)
                target = page
            else:
                target = (
                    PAGE_NAV_CALLBACKS.get(callback_id)
                    or DRILLDOWN_CALLBACKS.get(callback_id)
                    or V4_FAVORITE_PAGE_CALLBACKS.get(callback_id)
                    or V4_CONTEXT_CALLBACKS.get(callback_id)
                )
                if target == HOME_PAGE:
                    _v4_remember_recent(state, page)
                    state.history.clear()
                elif target and target != page:
                    _v4_remember_recent(state, page)
                    state.history.append(page)
            state.current_page = target or HOME_PAGE
            response = _v4_page_response(self, state.current_page, state)
            result_status = "ok"
    except Exception as exc:
        LOGGER.exception("telegram console v4 page failed callback=%s user_id=%s", callback_id, telegram_user_id)
        response = "error: command failed safely"
        result_status = "error"
        error_message = str(exc)
    audit_telegram_command(
        self.config.audit_db_path,
        telegram_user_id=telegram_user_id,
        command=f"callback:{callback_id}",
        args="",
        allowed=allowed,
        result_status=result_status,
        error_message=error_message if result_status == "error" else ("" if allowed else str(response)),
    )
    return response if isinstance(response, TelegramResponse) else TelegramResponse(safe_telegram_text(response, token=self.config.bot_token))


def _v4_handle_callback(self: TelegramConsole, telegram_user_id: int, callback_data: str) -> TelegramResponse:
    callback_id = normalize_callback_id(callback_data)
    if self._is_page_callback(callback_id):
        return _v4_handle_page_callback(self, telegram_user_id, callback_id)
    return _v3_handle_callback(self, telegram_user_id, callback_data)


def _v4_handle_console_callback(
    self: TelegramConsole,
    chat_id: int,
    telegram_user_id: int,
    callback_data: str,
) -> TelegramResponse:
    return _v4_handle_callback(self, telegram_user_id, callback_data)


def _v4_handle_text(self: TelegramConsole, telegram_user_id: int, text: str) -> TelegramResponse:
    command, _args = split_command(text)
    if command in {"/start", "/console"}:
        state = _v4_state(self, telegram_user_id)
        state.current_page = HOME_PAGE
        state.history.clear()
    return _v3_handle_text(self, telegram_user_id, text)


TelegramConsole.__init__ = _v4_console_init
TelegramConsole._is_page_callback = _v4_is_page_callback
TelegramConsole._page_context = _v4_page_context
TelegramConsole._page_response = _v4_page_response
TelegramConsole.handle_callback = _v4_handle_callback
TelegramConsole.handle_console_callback = _v4_handle_console_callback
TelegramConsole.handle_text = _v4_handle_text


V4_CONTEXT_CALLBACKS["quick_trades_log"] = "trades_log"
V4_CONTEXT_CALLBACKS["quick_outcome_validation"] = "outcome_validation"
V4_PAGE_TITLES["trades_log"] = "📜 Trades Log"
V4_PAGE_TITLES["portfolio_analytics"] = "📊 Portfolio Analytics"
V4_PAGE_TITLES["outcome_validation"] = "📈 Outcome Validation"
_v4_render_console_page_with_navigation = render_console_page
_v4_page_reply_markup_with_navigation = page_reply_markup


def _paper_trades_log_details(paper: dict[str, Any]) -> list[str]:
    rows: list[str] = []
    for trade in (paper.get("recent_trades") or [])[:8]:
        if not isinstance(trade, dict):
            continue
        rows.append(
            f"{trade.get('status', 'unknown')} | {trade.get('strategy', 'paper')} | "
            f"{_format_money(trade.get('pnl'))} | stake=${float(trade.get('stake') or trade.get('notional') or 0):.2f}"
        )
        rows.append(str(trade.get("market_title") or "unknown")[:90])
    return rows or ["No canonical paper trades recorded."]


def _outcome_validation_details(paper: dict[str, Any]) -> list[str]:
    return outcome_validation_report_text(paper).split("\n")[1:]


def render_console_page(
    page: str,
    *,
    config: TelegramConsoleConfig,
    context: ConsolePageContext,
    history: list[str] | None = None,
    favorites: list[str] | None = None,
    recent_pages: list[str] | None = None,
) -> str:
    if page == "portfolio_analytics":
        body = _v3_render_detail_page(
            "📊 Portfolio Analytics",
            portfolio_analytics_report_text(context.paper).split("\n")[1:],
            context.now,
        )
        return _v4_wrap_page(body, page=page, history=list(history or []), favorites=list(favorites or []), recent_pages=list(recent_pages or []))
    if page == "outcome_validation":
        body = _v3_render_detail_page("📈 Outcome Validation", _outcome_validation_details(context.paper), context.now)
        return _v4_wrap_page(body, page=page, history=list(history or []), favorites=list(favorites or []), recent_pages=list(recent_pages or []))
    if page == "performance":
        body = _v3_render_metric_page(
            "📈 Performance",
            [],
            context.now,
            sections=[
                (
                    "Portfolio",
                    [
                        ("Starting Bankroll", _format_money(context.paper.get("starting_bankroll"))),
                        ("Current Equity", _format_money(context.paper.get("current_equity") or context.paper.get("total_equity"))),
                        ("Cash", _format_money(context.paper.get("cash_balance"))),
                        ("Open Position Value", _format_money(context.paper.get("open_position_value"))),
                    ],
                ),
                (
                    "PnL",
                    [
                        ("Realized PnL", _format_money(context.paper.get("realized_pnl"))),
                        ("Unrealized PnL", _format_money(context.paper.get("unrealized_pnl"))),
                        ("Total PnL", _format_money(context.paper.get("total_pnl"))),
                        ("ROI", format_pct_metric(context.paper.get("roi_pct"))),
                    ],
                ),
                (
                    "Trading",
                    [
                        ("Open Positions", format_count_metric(context.paper.get("open_positions_count"))),
                        ("Closed Trades", format_count_metric(context.paper.get("closed_positions_count"))),
                        ("Win Rate", format_pct_metric(context.paper.get("win_rate"))),
                        ("Top Strategy", _strategy_label(context.paper.get("top_strategy"))),
                    ],
                ),
                (
                    "Execution Quality",
                    [
                        ("Fill Rate", format_pct_metric((context.paper.get("execution_quality") or {}).get("fill_rate"))),
                        ("Avg Slippage", _format_money((context.paper.get("execution_quality") or {}).get("average_slippage"))),
                        ("Rejection Rate", format_pct_metric((context.paper.get("execution_quality") or {}).get("rejection_rate"))),
                    ],
                ),
            ],
        )
        return _v4_wrap_page(body, page=page, history=list(history or []), favorites=list(favorites or []), recent_pages=list(recent_pages or []))
    if page == "trades_log":
        body = _v3_render_detail_page("📜 Trades Log", _paper_trades_log_details(context.paper), context.now)
        return _v4_wrap_page(body, page=page, history=list(history or []), favorites=list(favorites or []), recent_pages=list(recent_pages or []))
    return _v4_render_console_page_with_navigation(
        page,
        config=config,
        context=context,
        history=history,
        favorites=favorites,
        recent_pages=recent_pages,
    )


def page_reply_markup(page: str, state: ConsoleNavigationState | None = None) -> dict[str, Any]:
    markup = _v4_page_reply_markup_with_navigation(page, state)
    if page == HOME_PAGE:
        rows = list(markup["inline_keyboard"])
        rows.insert(2, [{"text": "📊 Portfolio Analytics", "callback_data": "nav_portfolio"}])
        rows.insert(3, [{"text": "📈 Outcome Validation", "callback_data": "nav_outcome"}])
        markup = {"inline_keyboard": rows}
    if page == "performance":
        rows = list(markup["inline_keyboard"])
        rows.insert(2, [{"text": "📜 Trades Log", "callback_data": "quick_trades_log"}])
        markup = {"inline_keyboard": rows}
    return markup
