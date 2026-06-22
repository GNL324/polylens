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

    def handle_text(self, telegram_user_id: int, text: str) -> str:
        raw = str(text or "").strip()
        command, args = split_command(raw)
        allowed = self._is_allowed(telegram_user_id)
        result_status = "ok"
        error_message = ""
        try:
            if not allowed:
                result_status = "rejected"
                response = "admin allowlist missing" if not self.config.admin_user_ids else "unauthorized"
            elif command in LIVE_COMMANDS:
                result_status = "blocked"
                response = "live trading disabled"
            else:
                response = self._dispatch(command, args)
        except Exception as exc:
            LOGGER.exception("telegram console command failed command=%s user_id=%s", command, telegram_user_id)
            result_status = "error"
            error_message = str(exc)
            response = "error: command failed safely"
        response = safe_telegram_text(response, token=self.config.bot_token)
        audit_telegram_command(
            self.config.audit_db_path,
            telegram_user_id=telegram_user_id,
            command=command,
            args=args,
            allowed=allowed,
            result_status=result_status,
            error_message=error_message,
        )
        return response

    def poll_once(self, *, offset: int | None = None, timeout: int = 30) -> int | None:
        self.validate_startup()
        payload = self._telegram_request(
            "getUpdates",
            {
                "timeout": max(1, int(timeout)),
                **({"offset": offset} if offset is not None else {}),
                "allowed_updates": json.dumps(["message"]),
            },
        )
        next_offset = offset
        for update in payload.get("result", []):
            next_offset = int(update["update_id"]) + 1
            message = update.get("message") or {}
            text = str(message.get("text") or "")
            chat = message.get("chat") or {}
            from_user = message.get("from") or {}
            chat_id = chat.get("id")
            user_id = from_user.get("id")
            if chat_id is None or user_id is None or not text.startswith("/"):
                continue
            response = self.handle_text(int(user_id), text)
            self._telegram_request("sendMessage", {"chat_id": chat_id, "text": response})
        return next_offset

    def run_forever(self, *, poll_timeout: int = 30, sleep_seconds: float = 1.0) -> None:
        self.validate_startup()
        LOGGER.info("starting telegram console config=%s", self.config.safe_dict())
        offset: int | None = None
        while True:
            offset = self.poll_once(offset=offset, timeout=poll_timeout)
            time.sleep(max(0.0, sleep_seconds))

    def _dispatch(self, command: str, args: str) -> str:
        if command in {"", "/start", "/help"}:
            return help_text()
        if command == "/status":
            return (
                "Status: read-only; "
                f"paper-only={str(self.config.paper_only).lower()}; "
                f"live={str(self.config.live_enabled and not self.config.paper_only).lower()}"
            )
        if command == "/health":
            return format_health(self.health_provider())
        if command == "/signals":
            return format_signals(self.signals_provider())
        if command == "/top_wallets":
            return format_top_wallets(self.top_wallets_provider())
        if command == "/wallet":
            return self._wallet(args)
        if command == "/paper_status":
            return format_paper_status(self.paper_provider())
        if command == "/risk":
            return format_risk(self.risk_provider())
        return "unknown command. Try /help"

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
                error_message TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_telegram_command_audit_timestamp
                ON telegram_command_audit(timestamp_utc);
            CREATE INDEX IF NOT EXISTS idx_telegram_command_audit_user
                ON telegram_command_audit(telegram_user_id);
            """
        )


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


def help_text() -> str:
    return "Polylens Telegram console\nCommands:\n" + "\n".join(COMMANDS)


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
