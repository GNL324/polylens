from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from src.analysis.paper_intelligence import paper_trading_intelligence
from src.analysis.paper_trading_engine import DEFAULT_PAPER_TRADING_DB
from src.analysis.short_crypto_paper import DEFAULT_DB_PATH as DEFAULT_SHORT_CRYPTO_PAPER_DB
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
POLYMARKET_ANALYTICS_TRADER_BASE_URL = "https://polymarketanalytics.com/traders"
WALLET_RE = re.compile(r"^0x[a-fA-F0-9]{40}$")


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
        return self.send_notification("wallet_promotion", text, buttons=_wallet_buttons([event.get("wallet")]))

    def send_wallet_discovery(self, event: dict[str, Any]) -> dict[str, Any]:
        text = format_wallet_discovery(event)
        wallets = event.get("wallets") or event.get("new_wallets") or []
        return self.send_notification("wallet_discovery", text, buttons=_wallet_buttons(wallets))

    def send_wallet_autonomy_failure(self, event: dict[str, Any]) -> dict[str, Any]:
        text = format_wallet_autonomy_failure(event)
        return self.send_notification("wallet_autonomy_failure", text, buttons=_system_buttons())

    def send_system_health_alert(self, event: dict[str, Any]) -> dict[str, Any]:
        text = format_system_health_alert(event)
        return self.send_notification("system_health_alert", text, buttons=_system_buttons())

    def send_deployment_success(self, report: dict[str, Any], *, report_url: str | None = None) -> dict[str, Any]:
        from src.cicd.telegram import deployment_buttons, format_deployment_success

        return self.send_notification(
            "deployment_success",
            format_deployment_success(report),
            buttons=deployment_buttons(report_url),
        )

    def send_deployment_failure(self, report: dict[str, Any], *, report_url: str | None = None) -> dict[str, Any]:
        from src.cicd.telegram import deployment_buttons, format_deployment_failure

        return self.send_notification(
            "deployment_failure",
            format_deployment_failure(report),
            buttons=deployment_buttons(report_url),
        )

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
    short_crypto_db_path: str | Path = DEFAULT_SHORT_CRYPTO_PAPER_DB,
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
    paper = paper_trading_intelligence(db_path=paper_db_path, short_crypto_db_path=short_crypto_db_path, limit=10)
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
            "daily_pnl": paper.get("daily_pnl", 0.0),
            "pnl_7d": paper.get("pnl_7d", 0.0),
            "total_pnl": paper.get("total_pnl", 0.0),
            "open_positions": paper.get("open_positions_count", 0),
            "closed_positions": paper.get("closed_positions_count", 0),
            "win_rate": paper.get("win_rate", 0.0),
            "trade_count": paper.get("trade_count", 0),
            "recent_trades": paper.get("recent_trades", [])[:3],
            "strategy_breakdown": paper.get("strategy_breakdown", {}),
            "top_strategy": paper.get("top_strategy"),
            "worst_strategy": paper.get("worst_strategy"),
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
        f"Daily PnL: {_money_signed(paper.get('daily_pnl'))}",
        f"7D PnL: {_money_signed(paper.get('pnl_7d'))}",
        f"Total PnL: {_money_signed(paper.get('total_pnl'))}",
        f"Open positions: {int(paper.get('open_positions') or 0)}",
        f"Closed positions: {int(paper.get('closed_positions') or 0)}",
        f"Win rate: {_pct(paper.get('win_rate'))}",
        "Recent: " + _recent_trade_summary(paper.get("recent_trades", []), limit=3),
        "Top strategy: " + _strategy_summary(paper.get("top_strategy")),
        "Worst strategy: " + _strategy_summary(paper.get("worst_strategy")),
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
    recent_discoveries = discovery.load_recent_discoveries(limit=10)
    promotions = performance.load_actions(action="promoted", limit=10)
    demotions = performance.load_actions(action="probation", limit=10)
    retirements = performance.load_actions(action="retired", limit=10)
    wallets = _valid_wallets_from_items([*recent_discoveries, *promotions])[:3]
    return "\n".join(
        [
            "Wallet Summary",
            f"New discoveries: {len(recent_discoveries)}",
            f"Promotions: {len(promotions)}",
            f"Demotions: {len(demotions)}",
            f"Retirements: {len(retirements)}",
            "Wallets: " + (", ".join(wallets) if wallets else "none"),
        ]
    )


def wallet_summary_link_buttons(
    *,
    traders_db_path: str | Path = DEFAULT_TRADERS_DB,
    discovery_db_path: str | Path = DEFAULT_TRADER_DISCOVERY_DB,
) -> list[list[dict[str, str]]]:
    discovery = WalletDiscoveryEngine(traders_db_path=traders_db_path, discovery_db_path=discovery_db_path)
    performance = WalletPerformanceEngine(traders_db_path=traders_db_path, discovery_db_path=discovery_db_path)
    wallets = _valid_wallets_from_items(
        [
            *discovery.load_recent_discoveries(limit=10),
            *performance.load_actions(action="promoted", limit=10),
        ]
    )
    return polymarket_analytics_wallet_buttons(wallets, limit=3)


def paper_performance_report_text(report: dict[str, Any] | None = None, db_path: str | Path = DEFAULT_PAPER_TRADING_DB) -> str:
    if isinstance(report, (str, Path)):
        db_path = report
        report = None
    paper = report or paper_trading_intelligence(db_path=db_path)
    return safe_telegram_text("\n".join(
        [
            "Paper Performance",
            f"Today: {_money_signed(paper.get('daily_pnl'))}",
            f"7D: {_money_signed(paper.get('pnl_7d'))}",
            f"Total: {_money_signed(paper.get('total_pnl'))}",
            f"Open positions: {int(paper.get('open_positions_count') or 0)}",
            f"Closed positions: {int(paper.get('closed_positions_count') or 0)}",
            f"Win rate: {_pct(paper.get('win_rate'))}",
            f"Trades: {int(paper.get('trade_count') or 0)}",
        ]
    ))


def portfolio_analytics_report_text(report: dict[str, Any] | None = None) -> str:
    """Compact, read-only canonical portfolio view for the Telegram console."""
    paper = report or paper_trading_intelligence()
    def label(item: Any) -> str:
        return str(item.get("label") or "N/A") if isinstance(item, dict) else "N/A"
    closed = [row for row in (paper.get("recent_trades") or []) if str(row.get("status") or "").lower() in {"closed", "expired"}]
    winners = [row for row in closed if float(row.get("realized_pnl") or 0.0) > 0][:3]
    losers = [row for row in closed if float(row.get("realized_pnl") or 0.0) < 0][:3]
    def compact(rows: list[dict[str, Any]]) -> str:
        if not rows:
            return "None"
        return ", ".join(f"{str(row.get('market_title') or row.get('market_id') or 'market')[:24]} {_money_signed(row.get('realized_pnl'))}" for row in rows)
    return safe_telegram_text(
        "\n".join(
            [
                "📊 Portfolio Analytics",
                f"Current equity: {_money_signed(paper.get('current_equity') or paper.get('total_equity'))}",
                f"ROI: {_pct(paper.get('roi_pct'))}",
                f"Drawdown: {_money_signed(paper.get('drawdown'))}",
                f"Exposure: {_money_signed(paper.get('exposure'))}",
                f"Best strategy: {label(paper.get('top_strategy'))}",
                f"Best wallet: {label(paper.get('best_wallet'))}",
                f"Recent winners: {compact(winners)}",
                f"Recent losers: {compact(losers)}",
            ]
        )
    )


def paper_recent_report_text(report: dict[str, Any] | None = None) -> str:
    paper = report or paper_trading_intelligence()
    rows = paper.get("recent_trades") or []
    if not rows:
        return "Recent Paper Trades\nNone"
    lines = ["Recent Paper Trades"]
    for index, row in enumerate(rows[:5], start=1):
        status = str(row.get("status") or "UNKNOWN")
        pnl = _money_signed(row.get("pnl")) if status != "OPEN" else f"unrealized {_money_signed(row.get('unrealized_pnl'))}"
        lines.append("")
        lines.append(f"{index}. {row.get('strategy', 'unknown')} | {status} | {pnl}")
        lines.append(f"   Market: {_short_text(row.get('market_title'), 80)}")
        if row.get("opened_at"):
            lines.append(f"   Opened: {_short_timestamp(row.get('opened_at'))}")
        if row.get("closed_at"):
            lines.append(f"   Closed: {_short_timestamp(row.get('closed_at'))}")
        if is_valid_wallet_address(row.get("wallet")):
            lines.append(f"   Wallet: {row.get('wallet')}")
    return safe_telegram_text("\n".join(lines))


def paper_pnl_report_text(report: dict[str, Any] | None = None) -> str:
    paper = report or paper_trading_intelligence()
    return safe_telegram_text(
        "\n".join(
            [
                "Paper PnL",
                "",
                f"Today: {_money_signed(paper.get('daily_pnl'))}",
                f"7D: {_money_signed(paper.get('pnl_7d'))}",
                f"Total: {_money_signed(paper.get('total_pnl'))}",
                f"Win rate: {_pct(paper.get('win_rate'))}",
                f"Trades: {int(paper.get('trade_count') or 0)}",
            ]
        )
    )


def paper_positions_report_text(report: dict[str, Any] | None = None) -> str:
    paper = report or paper_trading_intelligence()
    rows = paper.get("open_positions") or []
    lines = ["Open Paper Positions", f"Open: {int(paper.get('open_positions_count') or 0)}", f"Closed: {int(paper.get('closed_positions_count') or 0)}"]
    if not rows:
        lines.append("None")
    for index, row in enumerate(rows[:5], start=1):
        lines.append("")
        lines.append(f"{index}. {row.get('strategy', 'unknown')} | {_money_signed(row.get('unrealized_pnl'))}")
        lines.append(f"   Market: {_short_text(row.get('market_title'), 80)}")
        if row.get("opened_at"):
            lines.append(f"   Opened: {_short_timestamp(row.get('opened_at'))}")
    return safe_telegram_text("\n".join(lines))


def paper_strategies_report_text(report: dict[str, Any] | None = None) -> str:
    paper = report or paper_trading_intelligence()
    breakdown = paper.get("strategy_breakdown") or {}
    rows = [row for row in breakdown.values() if int(row.get("trade_count") or 0) > 0]
    rows.sort(key=lambda row: (float(row.get("realized_pnl") or 0) + float(row.get("unrealized_pnl") or 0)), reverse=True)
    lines = ["Paper Strategies"]
    if not rows:
        lines.append("No paper trades")
    for row in rows[:8]:
        pnl = float(row.get("realized_pnl") or 0) + float(row.get("unrealized_pnl") or 0)
        lines.append(
            f"{row.get('strategy', 'unknown')}: {_money_signed(pnl)} "
            f"trades={int(row.get('trade_count') or 0)} "
            f"win={_pct(row.get('win_rate'))}"
        )
    return safe_telegram_text("\n".join(lines))


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


def _wallet_buttons(wallets: list[Any] | tuple[Any, ...] | None = None) -> list[list[dict[str, str]]]:
    return [[{"text": "Wallet Summary", "callback_data": "report_wallets"}], *polymarket_analytics_wallet_buttons(wallets or [])]


def _system_buttons() -> list[list[dict[str, str]]]:
    return [[{"text": "System", "callback_data": "menu_system"}]]


def _report_buttons() -> list[list[dict[str, str]]]:
    return [[{"text": "Reports", "callback_data": "menu_reports"}]]


def is_valid_wallet_address(wallet: Any) -> bool:
    return bool(WALLET_RE.fullmatch(str(wallet or "").strip()))


def polymarket_analytics_wallet_url(wallet: Any) -> str | None:
    text = str(wallet or "").strip()
    if not is_valid_wallet_address(text):
        return None
    return f"{POLYMARKET_ANALYTICS_TRADER_BASE_URL}/{text}"


def polymarket_analytics_wallet_button(wallet: Any) -> dict[str, str] | None:
    url = polymarket_analytics_wallet_url(wallet)
    if not url:
        return None
    return {"text": "View on Polymarket Analytics", "url": url}


def polymarket_analytics_wallet_buttons(wallets: Iterable[Any], *, limit: int = 5) -> list[list[dict[str, str]]]:
    rows: list[list[dict[str, str]]] = []
    seen: set[str] = set()
    for wallet in wallets:
        text = str(wallet or "").strip()
        key = text.lower()
        if key in seen:
            continue
        button = polymarket_analytics_wallet_button(text)
        if button is None:
            continue
        seen.add(key)
        rows.append([button])
        if len(rows) >= limit:
            break
    return rows


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


def _valid_wallets_from_items(items: Iterable[Any]) -> list[str]:
    wallets: list[str] = []
    seen: set[str] = set()
    for item in items:
        payload = item.to_dict() if hasattr(item, "to_dict") else item
        if not isinstance(payload, dict):
            continue
        wallet = str(payload.get("wallet") or payload.get("address") or payload.get("trader_wallet") or "").strip()
        key = wallet.lower()
        if key in seen or not is_valid_wallet_address(wallet):
            continue
        seen.add(key)
        wallets.append(wallet)
    return wallets


def _recent_trade_summary(rows: list[dict[str, Any]], *, limit: int = 3) -> str:
    if not rows:
        return "none"
    parts = []
    for row in rows[:limit]:
        status = str(row.get("status") or "UNKNOWN")
        parts.append(f"{row.get('strategy', 'unknown')} {status} {_money_signed(row.get('pnl'))}")
    return "; ".join(parts)


def _strategy_summary(row: dict[str, Any] | None) -> str:
    if not row:
        return "n/a"
    pnl = float(row.get("realized_pnl") or 0) + float(row.get("unrealized_pnl") or 0)
    return f"{row.get('strategy', 'unknown')} {_money_signed(pnl)} ({int(row.get('trade_count') or 0)} trades)"


def _money(value: Any) -> str:
    try:
        return f"${float(value or 0):.2f}"
    except (TypeError, ValueError):
        return "$0.00"


def _money_signed(value: Any) -> str:
    try:
        number = float(value or 0)
    except (TypeError, ValueError):
        number = 0.0
    if number > 0:
        return f"+${number:.2f}"
    if number < 0:
        return f"-${abs(number):.2f}"
    return "$0.00"


def _pct(value: Any) -> str:
    try:
        return f"{float(value or 0) * 100:.1f}%"
    except (TypeError, ValueError):
        return "0.0%"


def _short_text(value: Any, limit: int) -> str:
    text = str(value or "unknown").replace("\n", " ").strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "..."


def _short_timestamp(value: Any) -> str:
    text = str(value or "")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return _short_text(text, 32)
    return parsed.strftime("%Y-%m-%d %H:%M UTC")


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
