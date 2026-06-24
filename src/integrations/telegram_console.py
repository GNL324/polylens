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
from src.analysis.paper_trading_service import paper_trading_health
from src.analysis.trader_registry import DEFAULT_TRADERS_DB, top_traders, trader_summary
from src.analysis.trader_signal_engine import DEFAULT_TRADER_SIGNAL_DB, trader_signal_health
from src.intelligence.wallet_service_health import wallet_service_health_summary
from src.integrations.telegram_notifications import (
    format_daily_intelligence_report,
    generate_daily_intelligence_report,
    paper_performance_report_text,
    signal_summary_report_text,
    wallet_summary_report_text,
)
from src.sqlite_utils import closing_connection
from src.trading.kill_switch import DEFAULT_READINESS_DB, KillSwitch

LOGGER = logging.getLogger(__name__)

DEFAULT_TELEGRAM_AUDIT_DB = DEFAULT_TRADERS_DB
TELEGRAM_API_BASE = "https://api.telegram.org"
MAX_TELEGRAM_TEXT = 3500
COMMANDS = (
    "/start",
    "/help",
    "/status",
    "/health",
    "/signals",
    "/top_wallets",
    "/wallet <address>",
    "/paper_status",
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
}
MENU_CALLBACKS = {
    "menu_main": "main",
    "menu_intelligence": "intelligence",
    "menu_wallets": "wallets",
    "menu_signals": "signals",
    "menu_system": "system",
    "menu_reports": "reports",
}
LIVE_CALLBACKS = {"buy", "sell", "order", "trade", "kill_switch", "resume_trading"}
WALLET_RE = re.compile(r"^0x[a-fA-F0-9]{40}$")


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
    readiness_db_path: str = DEFAULT_READINESS_DB

    @classmethod
    def from_env(cls, *, audit_db_path: str | None = None) -> "TelegramConsoleConfig":
        return cls(
            bot_token=os.environ.get("POLYLENS_TELEGRAM_BOT_TOKEN", ""),
            admin_user_ids=parse_admin_user_ids(os.environ.get("POLYLENS_TELEGRAM_ADMIN_USER_IDS", "")),
            paper_only=_env_bool("POLYLENS_TELEGRAM_PAPER_ONLY", default=True),
            live_enabled=_env_bool("POLYLENS_TELEGRAM_LIVE_ENABLED", default=False),
            audit_db_path=audit_db_path or os.environ.get("POLYLENS_TELEGRAM_AUDIT_DB", DEFAULT_TELEGRAM_AUDIT_DB),
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

    def __str__(self) -> str:
        return self.text

    def __contains__(self, needle: str) -> bool:
        return needle in self.text

    def __eq__(self, other: object) -> bool:
        if isinstance(other, str):
            return self.text == other
        return super().__eq__(other)


class TelegramConsole:
    def __init__(
        self,
        config: TelegramConsoleConfig,
        *,
        health_provider: Provider | None = None,
        signals_provider: Provider | None = None,
        paper_provider: Provider | None = None,
        risk_provider: Provider | None = None,
        top_wallets_provider: Callable[[], list[Any]] | None = None,
        wallet_provider: Callable[[str], dict[str, Any] | None] | None = None,
    ) -> None:
        self.config = config
        self.health_provider = health_provider or (lambda: wallet_service_health_summary(traders_db_path=DEFAULT_TRADERS_DB))
        self.signals_provider = signals_provider or (lambda: trader_signal_health(db_path=config.trader_signal_db_path))
        self.paper_provider = paper_provider or (lambda: paper_trading_health(db_path=config.paper_db_path))
        self.risk_provider = risk_provider or (lambda: KillSwitch(db_path=config.readiness_db_path).status())
        self.top_wallets_provider = top_wallets_provider or (lambda: top_traders(limit=5, db_path=DEFAULT_TRADERS_DB))
        self.wallet_provider = wallet_provider or (lambda wallet: trader_summary(wallet, db_path=DEFAULT_TRADERS_DB))
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
        return TelegramResponse(response, reply_markup=reply_markup)

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
                    response = self.handle_callback(int(user_id), callback_data)
                    self._edit_message_or_send(chat_id, message_id, response)
                continue
            message = update.get("message") or {}
            text = str(message.get("text") or "")
            chat = message.get("chat") or {}
            from_user = message.get("from") or {}
            chat_id = chat.get("id")
            user_id = from_user.get("id")
            if chat_id is None or user_id is None or not text.startswith("/"):
                continue
            response = self.handle_text(int(user_id), text)
            self._send_message(chat_id, response)
        return next_offset

    def run_forever(self, *, poll_timeout: int = 30, sleep_seconds: float = 1.0) -> None:
        self.validate_startup()
        LOGGER.info("starting telegram console config=%s", self.config.safe_dict())
        offset: int | None = None
        while True:
            offset = self.poll_once(offset=offset, timeout=poll_timeout)
            time.sleep(max(0.0, sleep_seconds))

    def _dispatch(self, command: str, args: str) -> TelegramResponse:
        if command in {"", "/start", "/help"}:
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
            return self._menu_response(format_top_wallets(self.top_wallets_provider()))
        if command == "/wallet":
            return TelegramResponse(self._wallet(args))
        if command == "/paper_status":
            return self._menu_response(format_paper_status(self.paper_provider()))
        if command == "/risk":
            return self._menu_response(format_risk(self.risk_provider()))
        if command == "/report_daily":
            return TelegramResponse(format_daily_intelligence_report(generate_daily_intelligence_report()), reply_markup=menu_reply_markup("reports"))
        if command == "/report_signals":
            return TelegramResponse(signal_summary_report_text(), reply_markup=menu_reply_markup("reports"))
        if command == "/report_wallets":
            return TelegramResponse(wallet_summary_report_text(), reply_markup=menu_reply_markup("reports"))
        if command == "/report_paper":
            return TelegramResponse(paper_performance_report_text(), reply_markup=menu_reply_markup("reports"))
        return self._menu_response("unknown command. Try /help")

    def _wallet(self, args: str) -> str:
        wallet = args.strip().split()[0] if args.strip() else ""
        if not WALLET_RE.match(wallet):
            return "wallet: provide a valid 0x address"
        summary = self.wallet_provider(wallet.lower())
        if not summary:
            return f"Wallet {short_wallet(wallet)}: not found"
        return (
            f"Wallet {short_wallet(wallet)}: "
            f"class={summary.get('classification', 'unknown')} "
            f"score={summary.get('watch_score', 0)} "
            f"confidence={float(summary.get('confidence') or 0):.2f} "
            f"reports={summary.get('report_count', 0)}"
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
        if response.reply_markup:
            params["reply_markup"] = json.dumps(response.reply_markup)
        return self._telegram_request("sendMessage", params)

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
        if message_id is not None:
            params: dict[str, Any] = {"chat_id": chat_id, "message_id": message_id, "text": response.text}
            if response.reply_markup:
                params["reply_markup"] = json.dumps(response.reply_markup)
            try:
                return self._telegram_request("editMessageText", params)
            except Exception:
                LOGGER.warning("telegram edit failed; falling back to sendMessage")
        return self._send_message(chat_id, response)

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
                [_back_button()],
            ]
        )
    return _menu_markup(
        [
            [
                {"text": "Intelligence", "callback_data": "menu_intelligence"},
                {"text": "Wallets", "callback_data": "menu_wallets"},
            ],
            [
                {"text": "Signals", "callback_data": "menu_signals"},
                {"text": "System", "callback_data": "menu_system"},
            ],
            [{"text": "Reports", "callback_data": "menu_reports"}],
        ]
    )


def main_menu_reply_markup() -> dict[str, Any]:
    return menu_reply_markup("main")


def _menu_markup(rows: list[list[dict[str, str]]]) -> dict[str, Any]:
    return {"inline_keyboard": rows}


def _back_button() -> dict[str, str]:
    return {"text": "Back", "callback_data": "menu_main"}


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
