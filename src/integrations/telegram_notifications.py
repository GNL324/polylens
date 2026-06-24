from __future__ import annotations

import json
import logging
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from src.analysis.paper_trading_engine import DEFAULT_PAPER_TRADING_DB, performance_report
from src.analysis.trader_registry import DEFAULT_TRADERS_DB
from src.analysis.trader_signal_engine import DEFAULT_TRADER_SIGNAL_DB, trader_signal_health, trader_signal_report
from src.intelligence.wallet_discovery import DEFAULT_TRADER_DISCOVERY_DB, WalletDiscoveryEngine
from src.intelligence.wallet_performance import WalletPerformanceEngine
from src.intelligence.wallet_service_health import wallet_service_health_summary
from src.sqlite_utils import closing_connection

LOGGER = logging.getLogger(__name__)

TELEGRAM_API_BASE = "https://api.telegram.org"
DEFAULT_TELEGRAM_AUDIT_DB = DEFAULT_TRADERS_DB
MAX_TELEGRAM_TEXT = 3500


@dataclass(frozen=True)
class TelegramNotificationConfig:
    bot_token: str
    chat_id: str
    notifications_enabled: bool = True
    daily_report_enabled: bool = True
    audit_db_path: str = DEFAULT_TELEGRAM_AUDIT_DB

    @classmethod
    def from_env(cls, *, audit_db_path: str | None = None) -> "TelegramNotificationConfig":
        return cls(
            bot_token=os.environ.get("POLYLENS_TELEGRAM_BOT_TOKEN", ""),
            chat_id=os.environ.get("POLYLENS_TELEGRAM_CHAT_ID", os.environ.get("TELEGRAM_CHAT_ID", "")),
            notifications_enabled=_env_bool("POLYLENS_TELEGRAM_NOTIFICATIONS_ENABLED", default=True),
            daily_report_enabled=_env_bool("POLYLENS_TELEGRAM_DAILY_REPORT_ENABLED", default=True),
            audit_db_path=audit_db_path or os.environ.get("POLYLENS_TELEGRAM_AUDIT_DB", DEFAULT_TELEGRAM_AUDIT_DB),
        )


class TelegramNotificationService:
    def __init__(
        self,
        config: TelegramNotificationConfig,
        *,
        request_sender: Callable[[str, dict[str, Any], str], dict[str, Any]] | None = None,
    ) -> None:
        self.config = config
        self.request_sender = request_sender or _telegram_request
        init_telegram_notification_audit_db(config.audit_db_path)

    def send_notification(
        self,
        notification_type: str,
        text: str,
        *,
        buttons: list[list[dict[str, str]]] | None = None,
        require_daily_enabled: bool = False,
    ) -> dict[str, Any]:
        enabled = self.config.notifications_enabled and (
            self.config.daily_report_enabled if require_daily_enabled else True
        )
        if not enabled:
            result = {"sent": False, "delivery_status": "disabled", "notification_type": notification_type}
            audit_notification_delivery(
                self.config.audit_db_path,
                notification_type=notification_type,
                notification_sent=False,
                delivery_status="disabled",
            )
            return result
        if not self.config.bot_token or not self.config.chat_id:
            result = {"sent": False, "delivery_status": "skipped_missing_config", "notification_type": notification_type}
            audit_notification_delivery(
                self.config.audit_db_path,
                notification_type=notification_type,
                notification_sent=False,
                delivery_status="skipped_missing_config",
                error_message="telegram token or chat id missing",
            )
            return result

        payload: dict[str, Any] = {
            "chat_id": self.config.chat_id,
            "text": safe_telegram_text(text, token=self.config.bot_token),
        }
        if buttons:
            payload["reply_markup"] = json.dumps({"inline_keyboard": buttons})
        try:
            response = self.request_sender("sendMessage", payload, self.config.bot_token)
            audit_notification_delivery(
                self.config.audit_db_path,
                notification_type=notification_type,
                notification_sent=True,
                delivery_status="sent",
            )
            return {
                "sent": True,
                "delivery_status": "sent",
                "notification_type": notification_type,
                "response": response,
            }
        except Exception:
            LOGGER.warning("telegram notification delivery failed type=%s", notification_type)
            audit_notification_delivery(
                self.config.audit_db_path,
                notification_type=notification_type,
                notification_sent=False,
                delivery_status="error",
                error_message="delivery failed",
            )
            return {"sent": False, "delivery_status": "error", "notification_type": notification_type}

    def send_high_conviction_signal(self, signal: dict[str, Any]) -> dict[str, Any]:
        text = format_high_conviction_signal(signal)
        return self.send_notification("high_conviction_signal", text, buttons=_signal_buttons())

    def send_wallet_promotion(self, event: dict[str, Any]) -> dict[str, Any]:
        text = format_wallet_promotion(event)
        return self.send_notification("wallet_promotion", text, buttons=_wallet_buttons())

    def send_wallet_discovery(self, event: dict[str, Any]) -> dict[str, Any]:
        text = format_wallet_discovery(event)
        return self.send_notification("wallet_discovery", text, buttons=_wallet_buttons())

    def send_wallet_autonomy_failure(self, event: dict[str, Any]) -> dict[str, Any]:
        text = format_wallet_autonomy_failure(event)
        return self.send_notification("wallet_autonomy_failure", text, buttons=_system_buttons())

    def send_system_health_alert(self, event: dict[str, Any]) -> dict[str, Any]:
        text = format_system_health_alert(event)
        return self.send_notification("system_health_alert", text, buttons=_system_buttons())

    def send_daily_report(self, report: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = report or generate_daily_intelligence_report()
        return self.send_notification(
            "daily_intelligence_report",
            format_daily_intelligence_report(payload),
            buttons=_report_buttons(),
            require_daily_enabled=True,
        )


def generate_daily_intelligence_report(
    *,
    traders_db_path: str | Path = DEFAULT_TRADERS_DB,
    discovery_db_path: str | Path = DEFAULT_TRADER_DISCOVERY_DB,
    signal_db_path: str | Path = DEFAULT_TRADER_SIGNAL_DB,
    paper_db_path: str | Path = DEFAULT_PAPER_TRADING_DB,
) -> dict[str, Any]:
    discovery = WalletDiscoveryEngine(
        traders_db_path=traders_db_path,
        discovery_db_path=discovery_db_path,
    )
    performance = WalletPerformanceEngine(
        traders_db_path=traders_db_path,
        discovery_db_path=discovery_db_path,
    )
    signals = trader_signal_report(db_path=signal_db_path)
    signal_health = trader_signal_health(db_path=signal_db_path)
    paper = performance_report(db_path=paper_db_path)
    health = wallet_service_health_summary(traders_db_path=str(traders_db_path))

    promotions = performance.load_actions(action="promoted", limit=10)
    demotions = performance.load_actions(action="probation", limit=10)
    retirements = performance.load_actions(action="retired", limit=10)
    return {
        "generated_at": utc_now(),
        "read_only": True,
        "paper_only": True,
        "wallet_intelligence": {
            "new_discoveries": discovery.load_recent_discoveries(limit=10),
            "promotions": promotions,
            "demotions": demotions,
            "retirements": retirements,
        },
        "signals": {
            "summary": signals.get("summary", {}),
            "by_signal_type": signals.get("by_signal_type", []),
            "performance": signals.get("performance", []),
            "health": signal_health,
        },
        "paper_trading": {
            "daily_pnl": paper.get("realized_pnl", 0.0),
            "open_positions": paper.get("open_positions", 0),
            "closed_positions": paper.get("closed_positions", 0),
            "win_rate": paper.get("win_rate", 0.0),
            "roi": paper.get("roi", 0.0),
        },
        "system_health": {
            "wallet_autonomy_status": health.get("status", "unknown"),
            "signal_engine_status": signal_health.get("status", "unknown"),
            "critical_warnings": _critical_warnings(health, signal_health),
            "stale_cycles": health.get("stale_cycles", []),
        },
    }


def format_daily_intelligence_report(report: dict[str, Any]) -> str:
    wallets = report.get("wallet_intelligence", {})
    signals = report.get("signals", {})
    paper = report.get("paper_trading", {})
    system = report.get("system_health", {})
    signal_summary = signals.get("summary", {})
    by_family = signals.get("by_signal_type", [])[:5]
    performance = signals.get("performance", [])[:5]
    warnings = system.get("critical_warnings") or []
    lines = [
        "Polylens Daily Intelligence Brief",
        f"Generated: {report.get('generated_at', 'unknown')}",
        "",
        "Wallet Intelligence",
        f"New discoveries: {len(wallets.get('new_discoveries') or [])}",
        f"Promotions: {len(wallets.get('promotions') or [])}",
        f"Demotions: {len(wallets.get('demotions') or [])}",
        f"Retirements: {len(wallets.get('retirements') or [])}",
        "",
        "Signals",
        f"Total: {int(signal_summary.get('total_signals') or 0)}",
        "Families: " + _compact_counts(by_family, "signal_type"),
        "Status: " + _compact_counts(performance, "outcome"),
        "",
        "Paper Trading",
        f"Daily PnL: {_money(paper.get('daily_pnl'))}",
        f"Open positions: {int(paper.get('open_positions') or 0)}",
        f"Closed positions: {int(paper.get('closed_positions') or 0)}",
        f"Win rate: {_pct(paper.get('win_rate'))}",
        "",
        "System Health",
        f"Wallet autonomy: {system.get('wallet_autonomy_status', 'unknown')}",
        f"Signal engine: {system.get('signal_engine_status', 'unknown')}",
        "Warnings: " + (", ".join(str(item) for item in warnings[:5]) if warnings else "none"),
    ]
    return safe_telegram_text("\n".join(lines))


def signal_summary_report_text(report: dict[str, Any] | None = None) -> str:
    payload = report or trader_signal_report()
    summary = payload.get("summary", {})
    return "\n".join(
        [
            "Signal Summary",
            f"Total: {int(summary.get('total_signals') or 0)}",
            f"Wallets: {int(summary.get('wallets') or 0)}",
            f"Markets: {int(summary.get('markets') or 0)}",
            "Families: " + _compact_counts(payload.get("by_signal_type", []), "signal_type"),
            "Status: " + _compact_counts(payload.get("performance", []), "outcome"),
        ]
    )


def wallet_summary_report_text(
    *,
    traders_db_path: str | Path = DEFAULT_TRADERS_DB,
    discovery_db_path: str | Path = DEFAULT_TRADER_DISCOVERY_DB,
) -> str:
    discovery = WalletDiscoveryEngine(traders_db_path=traders_db_path, discovery_db_path=discovery_db_path)
    performance = WalletPerformanceEngine(traders_db_path=traders_db_path, discovery_db_path=discovery_db_path)
    return "\n".join(
        [
            "Wallet Summary",
            f"New discoveries: {len(discovery.load_recent_discoveries(limit=10))}",
            f"Promotions: {len(performance.load_actions(action='promoted', limit=10))}",
            f"Demotions: {len(performance.load_actions(action='probation', limit=10))}",
            f"Retirements: {len(performance.load_actions(action='retired', limit=10))}",
        ]
    )


def paper_performance_report_text(db_path: str | Path = DEFAULT_PAPER_TRADING_DB) -> str:
    paper = performance_report(db_path=db_path)
    return "\n".join(
        [
            "Paper Performance",
            f"Daily PnL: {_money(paper.get('realized_pnl'))}",
            f"Open positions: {int(paper.get('open_positions') or 0)}",
            f"Closed positions: {int(paper.get('closed_positions') or 0)}",
            f"Win rate: {_pct(paper.get('win_rate'))}",
            f"ROI: {_pct(paper.get('roi'))}",
        ]
    )


def format_high_conviction_signal(signal: dict[str, Any]) -> str:
    return "\n".join(
        [
            "High-conviction signal",
            f"Strategy: {signal.get('strategy_name') or signal.get('strategy') or 'unknown'}",
            f"Confidence: {_pct(signal.get('confidence'))}",
            f"Market: {signal.get('market_title') or signal.get('market') or 'unknown'}",
            f"Timestamp: {signal.get('signal_timestamp') or signal.get('timestamp') or 'unknown'}",
        ]
    )


def format_wallet_promotion(event: dict[str, Any]) -> str:
    return "\n".join(
        [
            "Wallet promotion",
            f"Wallet: {event.get('wallet') or 'unknown'}",
            f"Performance: {event.get('performance_summary') or event.get('summary') or 'n/a'}",
            f"Reason: {event.get('promotion_reason') or event.get('reason') or 'n/a'}",
        ]
    )


def format_wallet_discovery(event: dict[str, Any]) -> str:
    wallets = event.get("wallets") or event.get("new_wallets") or []
    summary = event.get("summary_metrics") or event.get("metrics") or {}
    return "\n".join(
        [
            "Wallet discovery",
            f"New wallets: {len(wallets)}",
            f"Wallets: {', '.join(str(wallet) for wallet in wallets[:5]) if wallets else 'none'}",
            f"Summary: {_compact_dict(summary)}",
        ]
    )


def format_wallet_autonomy_failure(event: dict[str, Any]) -> str:
    return "\n".join(
        [
            "Wallet autonomy failure",
            f"Cycle: {event.get('failed_cycle') or event.get('cycle') or 'unknown'}",
            f"Error: {event.get('error_summary') or event.get('error') or 'unknown'}",
            f"Timestamp: {event.get('timestamp') or utc_now()}",
        ]
    )


def format_system_health_alert(event: dict[str, Any]) -> str:
    warnings = event.get("critical_warnings") or event.get("warnings") or []
    return "\n".join(
        [
            "System health alert",
            f"Service: {event.get('service') or 'unknown'}",
            f"Status: {event.get('status') or 'unhealthy'}",
            f"Stale cycle: {event.get('stale_cycle') or 'none'}",
            f"Warnings: {', '.join(str(item) for item in warnings[:5]) if warnings else 'none'}",
        ]
    )


def init_telegram_notification_audit_db(db_path: str | Path = DEFAULT_TELEGRAM_AUDIT_DB) -> None:
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
            """
        )
        _ensure_column(conn, "telegram_command_audit", "notification_sent", "INTEGER")
        _ensure_column(conn, "telegram_command_audit", "notification_type", "TEXT")
        _ensure_column(conn, "telegram_command_audit", "delivery_status", "TEXT")
        conn.executescript(
            """
            CREATE INDEX IF NOT EXISTS idx_telegram_command_audit_notification
                ON telegram_command_audit(notification_type, delivery_status);
            """
        )


def audit_notification_delivery(
    db_path: str | Path,
    *,
    notification_type: str,
    notification_sent: bool,
    delivery_status: str,
    error_message: str = "",
) -> None:
    init_telegram_notification_audit_db(db_path)
    with closing_connection(db_path) as conn:
        conn.execute(
            """
            INSERT INTO telegram_command_audit (
                timestamp_utc, telegram_user_id, command, args, allowed,
                result_status, error_message, notification_sent,
                notification_type, delivery_status
            ) VALUES (?, 0, ?, '', 1, ?, ?, ?, ?, ?)
            """,
            (
                utc_now(),
                f"notification:{notification_type}",
                delivery_status,
                error_message or None,
                1 if notification_sent else 0,
                notification_type,
                delivery_status,
            ),
        )


def safe_telegram_text(text: str, *, token: str = "") -> str:
    safe = str(text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if token:
        safe = safe.replace(token, "[redacted]")
    if len(safe) > MAX_TELEGRAM_TEXT:
        safe = safe[: MAX_TELEGRAM_TEXT - 20].rstrip() + "\n[truncated]"
    return safe or "ok"


def _telegram_request(method: str, params: dict[str, Any], token: str) -> dict[str, Any]:
    url = f"{TELEGRAM_API_BASE}/bot{token}/{method}"
    body = urlencode(params).encode("utf-8")
    request = Request(url, data=body, headers={"Content-Type": "application/x-www-form-urlencoded"})
    with urlopen(request, timeout=35) as response:
        text = response.read().decode("utf-8")
    return json.loads(text) if text else {}


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def _env_bool(name: str, *, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _signal_buttons() -> list[list[dict[str, str]]]:
    return [[{"text": "Signal Summary", "callback_data": "report_signals"}]]


def _wallet_buttons() -> list[list[dict[str, str]]]:
    return [[{"text": "Wallet Summary", "callback_data": "report_wallets"}]]


def _system_buttons() -> list[list[dict[str, str]]]:
    return [[{"text": "System", "callback_data": "menu_system"}]]


def _report_buttons() -> list[list[dict[str, str]]]:
    return [[{"text": "Reports", "callback_data": "menu_reports"}]]


def _critical_warnings(health: dict[str, Any], signal_health: dict[str, Any]) -> list[str]:
    warnings: list[str] = []
    if health.get("status") in {"unhealthy", "degraded"}:
        warnings.append(f"wallet autonomy {health.get('status')}")
    for cycle in health.get("stale_cycles") or []:
        warnings.append(f"stale cycle {cycle}")
    if signal_health.get("status") in {"error", "unhealthy"}:
        warnings.append(f"signal engine {signal_health.get('status')}")
    return warnings


def _compact_counts(rows: list[dict[str, Any]], key: str) -> str:
    if not rows:
        return "none"
    return ", ".join(f"{row.get(key, 'unknown')}={int(row.get('count') or 0)}" for row in rows[:5])


def _compact_dict(payload: dict[str, Any]) -> str:
    if not payload:
        return "none"
    return ", ".join(f"{key}={value}" for key, value in list(payload.items())[:5])


def _money(value: Any) -> str:
    try:
        return f"${float(value or 0):.2f}"
    except (TypeError, ValueError):
        return "$0.00"


def _pct(value: Any) -> str:
    try:
        return f"{float(value or 0) * 100:.1f}%"
    except (TypeError, ValueError):
        return "0.0%"


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
