from __future__ import annotations

from pathlib import Path
from typing import Any

from src.web.db import POLYLENS_DB_PATH
from src.web.mission_control_data import (
    REFRESH_SECONDS,
    SHORT_CRYPTO_PAPER_DB_PATH,
    format_pct,
    format_pnl,
    format_price,
    mission_control_snapshot,
)
from src.web.mission_control_styles import MISSION_CONTROL_CSS


def create_mission_control_page(
    *,
    paper_db_path: str | Path = SHORT_CRYPTO_PAPER_DB_PATH,
    polylens_db_path: str | Path = POLYLENS_DB_PATH,
) -> None:
    from nicegui import ui

    @ui.page("/mission-control")
    def mission_control_page() -> None:
        ui.add_head_html(f"<style>{MISSION_CONTROL_CSS}</style>")
        shell = ui.column().classes("mc-shell mc-body w-full")
        state: dict[str, Any] = {"snapshot": mission_control_snapshot(paper_db_path=paper_db_path, polylens_db_path=polylens_db_path)}

        def refresh() -> None:
            state["snapshot"] = mission_control_snapshot(paper_db_path=paper_db_path, polylens_db_path=polylens_db_path)
            render()

        def render() -> None:
            shell.clear()
            data = state["snapshot"]
            with shell:
                _render_header(data)
                _render_kpis(data["kpis"])
                with ui.element("div").classes("mc-grid-3"):
                    _render_pnl_chart(data["pnl_chart"])
                    _render_btc_feed(data["btc_feed"])
                    _render_pipeline(data["pipeline"])
                with ui.element("div").classes("mc-grid-bottom"):
                    _render_active_bet(data["active_bet"])
                    _render_recent_settlements(data["recent_settlements"])
                    _render_risk(data["risk"])
                _render_system_health(data["system_health"])
                with ui.element("div").classes("mc-footer"):
                    ui.html(f'<span>Auto-refresh {REFRESH_SECONDS}s • {data["generated_at"]}</span>', sanitize=False)
                    ui.link("Command Center", "/").classes("mc-link")

        render()
        ui.timer(REFRESH_SECONDS, refresh)


def _render_header(data: dict[str, Any]) -> None:
    from nicegui import ui

    header = data["header"]
    badges = data["mode_badges"]
    with ui.element("div").classes("mc-header"):
        with ui.element("div"):
            ui.html(
                f'<div class="mc-title">{header["title"]} • {header["mode_label"]} • {header["utc_time"]}</div>'
                f'<div class="mc-subtitle">{header["wallet"]} • {header["venue"]} • {header["strategy_label"]}</div>',
                sanitize=False,
            )
        with ui.element("div").classes("mc-badges"):
            _badge("PAPER", badges.get("paper"), "paper")
            _badge("LIVE_READY", badges.get("live_ready"), "live-ready")
            _badge("LIVE_BLOCKED", badges.get("live_blocked"), "live-blocked")
            _badge("KILL_SWITCH", badges.get("kill_switch"), "kill-switch")


def _badge(label: str, active: bool, css_class: str) -> None:
    from nicegui import ui

    classes = f"mc-badge {css_class}" if active else "mc-badge"
    opacity = "" if active else ' style="opacity:0.45"'
    ui.html(f'<span class="{classes}"{opacity}>{label}</span>', sanitize=False)


def _render_kpis(kpis: dict[str, Any]) -> None:
    from nicegui import ui

    realized = float(kpis.get("realized_pnl") or 0.0)
    unrealized = float(kpis.get("unrealized_pnl") or 0.0)
    streak = kpis.get("current_streak") or {"type": "none", "count": 0}
    streak_label = "—" if streak.get("type") == "none" else f'{streak["count"]} {str(streak["type"]).upper()}'

    items = [
        ("Realized PnL", format_pnl(realized), _pnl_class(realized)),
        ("Unrealized PnL", format_pnl(unrealized), _pnl_class(unrealized)),
        ("Total Trades", str(kpis.get("total_trades") or 0), ""),
        ("Win Rate", format_pct(kpis.get("win_rate")), "signal"),
        ("Trades/Day", str(kpis.get("trades_per_day") if kpis.get("trades_per_day") is not None else "—"), ""),
        ("Streak", streak_label, _streak_class(streak)),
        ("Daily Loss Left", format_pnl(kpis.get("daily_loss_remaining")), "signal"),
        ("Bankroll", f'${float(kpis.get("bankroll") or 0):.0f} / ${float(kpis.get("available_cash") or 0):.0f} free', ""),
    ]
    with ui.element("div").classes("mc-grid-hero"):
        for label, value, css in items:
            with ui.element("div").classes("mc-card mc-kpi"):
                ui.html(f'<div class="label">{label}</div><div class="value {css}">{value}</div>', sanitize=False)


def _render_pnl_chart(chart: dict[str, Any]) -> None:
    from nicegui import ui

    with ui.element("div").classes("mc-card"):
        ui.html('<div class="mc-card-title">24h PnL</div>', sanitize=False)
        ui.html(f'<div class="mc-chart-wrap">{chart.get("svg") or ""}</div>', sanitize=False)
        ui.html(
            '<div class="mc-chart-meta">'
            f'<span>Peak <strong>{format_pnl(chart.get("peak"))}</strong></span>'
            f'<span>Drawdown <strong>{format_pnl(chart.get("drawdown"))}</strong></span>'
            f'<span>Current <strong>{format_pnl(chart.get("current_pnl"))}</strong></span>'
            "</div>",
            sanitize=False,
        )


def _render_btc_feed(feed: dict[str, Any]) -> None:
    from nicegui import ui

    price = feed.get("price")
    change = feed.get("change_1h_pct")
    change_text = "—" if change is None else f"{change:+.2f}%"
    change_class = _pnl_class(change if change is not None else 0.0)
    with ui.element("div").classes("mc-card"):
        ui.html('<div class="mc-card-title">BTC Live Spot</div>', sanitize=False)
        ui.html(
            f'<div class="mc-kpi"><div class="value signal">{format_price(price)}</div>'
            f'<div class="label {change_class}">1h {change_text}</div></div>',
            sanitize=False,
        )
        ui.html(f'<div class="mc-chart-wrap">{feed.get("svg") or ""}</div>', sanitize=False)
        markers = feed.get("markers") or []
        marker_text = f"{len(markers)} trade markers (1h)" if markers else "no recent entries"
        ui.html(f'<div class="mc-chart-meta"><span>{marker_text}</span></div>', sanitize=False)


def _render_pipeline(stages: list[dict[str, Any]]) -> None:
    from nicegui import ui

    with ui.element("div").classes("mc-card"):
        ui.html('<div class="mc-card-title">6-Cycle Execution Pipeline</div>', sanitize=False)
        with ui.element("div").classes("mc-pipeline"):
            for stage in stages:
                state = stage.get("state") or "idle"
                latency = stage.get("latency_ms")
                latency_text = f"{latency}ms" if latency is not None else "—"
                error = stage.get("last_error") or "—"
                ui.html(
                    f'<div class="mc-stage {state}">'
                    f'<div class="name">{stage.get("name")}</div>'
                    f'<div class="status">{stage.get("status")}</div>'
                    f'<div class="meta">Latency: {latency_text}<br/>{stage.get("detail")}<br/>Err: {error}</div>'
                    f"</div>",
                    sanitize=False,
                )


def _render_active_bet(bet: dict[str, Any] | None) -> None:
    from nicegui import ui

    with ui.element("div").classes("mc-card"):
        ui.html('<div class="mc-card-title">Active Bet</div>', sanitize=False)
        if not bet:
            ui.html('<div class="mc-empty">No open position</div>', sanitize=False)
            return
        side = str(bet.get("side") or "").lower()
        side_class = "side-up" if side in {"up", "yes", "higher"} else "side-down"
        ui.html(
            '<div class="mc-active-bet">'
            f'<div class="market">{bet.get("market_title")}</div>'
            f'<div><span class="{side_class}">{bet.get("side")}</span> @ {float(bet.get("entry_price") or 0):.2f}</div>'
            f'<div>Size {float(bet.get("size") or 0):.2f} • Edge {float(bet.get("expected_edge") or 0):.3f}</div>'
            f'<div>Expires {bet.get("expiry_countdown")}</div>'
            f'<div>Mark {format_pnl(bet.get("mark_value"))} • {bet.get("strategy_label") or "—"}</div>'
            "</div>",
            sanitize=False,
        )


def _render_recent_settlements(rows: list[dict[str, Any]]) -> None:
    from nicegui import ui

    with ui.element("div").classes("mc-card"):
        ui.html('<div class="mc-card-title">Recent Settlements</div>', sanitize=False)
        if not rows:
            ui.html('<div class="mc-empty">No settlements yet</div>', sanitize=False)
            return
        body = "".join(_settlement_row(row) for row in rows)
        ui.html(
            '<table class="mc-table"><thead><tr>'
            "<th>Time</th><th>Outcome</th><th>PnL</th><th>Market</th><th>Reason</th>"
            f"</tr></thead><tbody>{body}</tbody></table>",
            sanitize=False,
        )


def _settlement_row(row: dict[str, Any]) -> str:
    result = str(row.get("result") or "")
    css = "mc-outcome-won" if result == "won" else "mc-outcome-lost" if result == "lost" else ""
    pnl = float(row.get("pnl") or 0.0)
    return (
        "<tr>"
        f'<td>{_short_ts(row.get("settled_at"))}</td>'
        f'<td class="{css}">{result.upper() or "—"}</td>'
        f'<td class="{_pnl_class(pnl)}">{format_pnl(pnl)}</td>'
        f'<td>{_truncate(row.get("market"), 28)}</td>'
        f'<td>{_truncate(row.get("reason"), 20)}</td>'
        "</tr>"
    )


def _render_risk(risk: dict[str, Any]) -> None:
    from nicegui import ui

    gate = risk.get("live_send_gate") or {}
    items = [
        ("Kill Switch", "ACTIVE" if risk.get("kill_switch") else "OFF"),
        ("Max Stake", f'${float(risk.get("max_stake") or 0):.2f}'),
        ("Daily Loss Limit", f'${float(risk.get("daily_loss_limit") or 0):.2f}'),
        ("Duplicate Suppression", "ON" if risk.get("duplicate_suppression") else "OFF"),
        ("Stale Data Guard", f'{float(risk.get("stale_data_guard_secs") or 0):.1f}s'),
        ("Liquidity Guard", f'${float(risk.get("liquidity_guard_min") or 0):.2f}'),
        ("Close Cutoff", f'{float(risk.get("close_cutoff_secs") or 0):.0f}s'),
        ("Live Send Gate", f'kalshi={gate.get("kalshi")} poly={gate.get("polymarket")}'),
    ]
    with ui.element("div").classes("mc-card"):
        ui.html('<div class="mc-card-title">Risk Engine</div>', sanitize=False)
        with ui.element("div").classes("mc-risk-grid"):
            for label, value in items:
                ui.html(
                    f'<div class="mc-risk-item"><div class="label">{label}</div><div class="value">{value}</div></div>',
                    sanitize=False,
                )


def _render_system_health(health: dict[str, Any]) -> None:
    from nicegui import ui

    api = health.get("api_connectivity") or {}
    rows = [
        ("Short Crypto Timer", health.get("short_crypto_timer"), _service_class(health.get("short_crypto_timer"))),
        ("Settle Timer", health.get("short_crypto_settle_timer"), _service_class(health.get("short_crypto_settle_timer"))),
        ("Dashboard Service", health.get("dashboard_service"), _service_class(health.get("dashboard_service"))),
        ("Last Scan", health.get("last_scan_time") or "—", ""),
        ("Paper DB", _truncate(health.get("paper_db_path"), 42), ""),
        ("Polylens DB", _truncate(health.get("polylens_db_path"), 42), ""),
        ("Kalshi API", api.get("kalshi"), _api_class(api.get("kalshi"))),
        ("Polymarket API", api.get("polymarket"), _api_class(api.get("polymarket"))),
    ]
    with ui.element("div").classes("mc-card"):
        ui.html('<div class="mc-card-title">System Health</div>', sanitize=False)
        with ui.element("div").classes("mc-health-list"):
            for label, value, css in rows:
                ui.html(
                    f'<div class="mc-health-row"><span>{label}</span><span class="{css}">{value}</span></div>',
                    sanitize=False,
                )


def _pnl_class(value: float | None) -> str:
    if value is None:
        return ""
    if value > 0:
        return "profit"
    if value < 0:
        return "loss"
    return ""


def _streak_class(streak: dict[str, Any]) -> str:
    if streak.get("type") == "won":
        return "profit"
    if streak.get("type") == "lost":
        return "loss"
    return ""


def _service_class(state: str | None) -> str:
    if state == "active":
        return "ok"
    if state in {"inactive", "failed", "unknown"}:
        return "bad"
    return "warn"


def _api_class(state: str | None) -> str:
    if state == "ok":
        return "ok"
    if state == "error":
        return "bad"
    return "warn"


def _short_ts(value: Any) -> str:
    text = str(value or "")
    if "T" in text:
        return text.split("T", 1)[1].replace("Z", "")[:8]
    return text[:19]


def _truncate(value: Any, limit: int) -> str:
    text = str(value or "—")
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."
