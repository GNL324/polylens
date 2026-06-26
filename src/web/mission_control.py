from __future__ import annotations

import json
from html import escape
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


RECHARTS_HEAD = """
<script crossorigin src="https://unpkg.com/react@18/umd/react.production.min.js"></script>
<script crossorigin src="https://unpkg.com/react-dom@18/umd/react-dom.production.min.js"></script>
<script crossorigin src="https://unpkg.com/recharts/umd/Recharts.min.js"></script>
<script>
window.PolylensRecharts = window.PolylensRecharts || {
  renderLine: function (id, rows, config) {
    const mount = document.getElementById(id);
    if (!mount || !window.React || !window.ReactDOM || !window.Recharts) {
      window.setTimeout(function () { window.PolylensRecharts.renderLine(id, rows, config); }, 80);
      return;
    }
    const R = window.Recharts;
    const h = window.React.createElement;
    const color = config.color || '#57d68d';
    const grid = '#243241';
    const axis = '#8ea0b6';
    const tooltipStyle = { background: '#111923', border: '1px solid #26384a', borderRadius: 8, color: '#f5f8fb' };
    const chart = h(R.ResponsiveContainer, { width: '100%', height: config.height || 260 },
      h(R.LineChart, { data: rows, margin: { top: 12, right: 18, left: 0, bottom: 8 } },
        h(R.CartesianGrid, { stroke: grid, strokeDasharray: '3 3', vertical: false }),
        h(R.XAxis, { dataKey: config.xKey || 'label', tick: { fill: axis, fontSize: 11 }, axisLine: false, tickLine: false, minTickGap: 24 }),
        h(R.YAxis, { tick: { fill: axis, fontSize: 11 }, axisLine: false, tickLine: false, width: 54 }),
        h(R.Tooltip, { contentStyle: tooltipStyle, labelStyle: { color: '#f5f8fb' } }),
        h(R.Line, { type: 'monotone', dataKey: config.valueKey, stroke: color, strokeWidth: 3, dot: false, activeDot: { r: 4 } })
      )
    );
    window.ReactDOM.createRoot(mount).render(chart);
  }
};
</script>
"""

MISSION_CONTROL_NAV_ITEMS = ("Overview", "Paper Portfolio", "Outcome Validation", "Opportunities", "Wallets", "Signals", "Strategies", "System Health")
DEFAULT_MISSION_CONTROL_TAB = MISSION_CONTROL_NAV_ITEMS[0]


def resolve_mission_control_tab(selected: Any, tabs: tuple[str, ...] = MISSION_CONTROL_NAV_ITEMS) -> str:
    selected_label = str(selected or "").strip()
    return selected_label if selected_label in tabs else tabs[0]


def set_mission_control_tab(state: dict[str, Any], selected: Any) -> str:
    active_tab = resolve_mission_control_tab(selected)
    state["active_tab"] = active_tab
    return active_tab


def create_mission_control_page(
    *,
    paper_db_path: str | Path = SHORT_CRYPTO_PAPER_DB_PATH,
    polylens_db_path: str | Path = POLYLENS_DB_PATH,
) -> None:
    from nicegui import ui
    from starlette.responses import RedirectResponse

    @ui.page("/")
    def mission_control_root() -> RedirectResponse:
        return RedirectResponse("/mission-control", status_code=307)

    @ui.page("/mission-control")
    def mission_control_page() -> None:
        ui.add_head_html(f"<style>{MISSION_CONTROL_CSS}</style>{RECHARTS_HEAD}")
        shell = ui.column().classes("mc-shell mc-body w-full")
        state: dict[str, Any] = {
            "snapshot": mission_control_snapshot(paper_db_path=paper_db_path, polylens_db_path=polylens_db_path),
            "active_tab": DEFAULT_MISSION_CONTROL_TAB,
        }

        def refresh() -> None:
            state["snapshot"] = mission_control_snapshot(paper_db_path=paper_db_path, polylens_db_path=polylens_db_path)
            render()

        def render() -> None:
            shell.clear()
            data = state["snapshot"]
            with shell:
                _render_header(data)
                active_tab = set_mission_control_tab(state, state.get("active_tab"))
                with ui.tabs(value=active_tab).classes("mc-tabs") as tabs:
                    tabs.on_value_change(lambda event: set_mission_control_tab(state, event.value))
                    ui.tab("Overview")
                    ui.tab("Paper Portfolio")
                    ui.tab("Outcome Validation")
                    ui.tab("Opportunities")
                    ui.tab("Wallets")
                    ui.tab("Signals")
                    ui.tab("Strategies")
                    ui.tab("System Health")
                with ui.tab_panels(tabs, value=active_tab).classes("mc-tab-panels"):
                    with ui.tab_panel("Overview").classes("mc-tab-panel"):
                        _render_overview(data)
                    with ui.tab_panel("Paper Portfolio").classes("mc-tab-panel"):
                        _render_paper_portfolio(data)
                    with ui.tab_panel("Outcome Validation").classes("mc-tab-panel"):
                        _render_outcome_validation(data)
                    with ui.tab_panel("Opportunities").classes("mc-tab-panel"):
                        _render_opportunities(data)
                    with ui.tab_panel("Wallets").classes("mc-tab-panel"):
                        _render_wallets(data)
                    with ui.tab_panel("Signals").classes("mc-tab-panel"):
                        _render_signals(data)
                    with ui.tab_panel("Strategies").classes("mc-tab-panel"):
                        _render_strategies(data)
                    with ui.tab_panel("System Health").classes("mc-tab-panel"):
                        _render_system_health_page(data)
                with ui.element("div").classes("mc-footer"):
                    ui.html(f'<span>Auto-refresh {REFRESH_SECONDS}s / {_html(data["generated_at"])}</span>', sanitize=False)
                    ui.link("Command Center", "/").classes("mc-link")

        render()
        ui.timer(REFRESH_SECONDS, refresh)


def _render_header(data: dict[str, Any]) -> None:
    from nicegui import ui

    header = data["header"]
    badges = data["mode_badges"]
    mode_class = "live" if badges.get("live_ready") else "paper"
    with ui.element("section").classes("mc-header"):
        with ui.element("div").classes("mc-brand"):
            ui.html(
                f'<div class="mc-eyebrow">Polylens Mission Control</div>'
                f'<h1>{_html(header["title"])} Intelligence</h1>'
                f'<p>{_html(header["strategy_label"])} / {_html(header["venue"])} / {_html(header["utc_time"])}</p>',
                sanitize=False,
            )
        with ui.element("div").classes("mc-header-side"):
            ui.html(f'<div class="mc-mode-pill {mode_class}">{_html(header["mode_label"])}</div>', sanitize=False)
            with ui.element("div").classes("mc-badges"):
                _badge("Paper Only", badges.get("paper"), "paper")
                _badge("Live Ready", badges.get("live_ready"), "live-ready")
                _badge("Live Blocked", badges.get("live_blocked"), "live-blocked")
                _badge("Kill Switch", badges.get("kill_switch"), "kill-switch")


def _render_overview(data: dict[str, Any]) -> None:
    from nicegui import ui

    _render_kpis(data["kpis"])
    with ui.element("div").classes("mc-dashboard-grid mc-overview-grid"):
        with ui.element("div").classes("mc-span-8"):
            _render_pnl_chart(data["pnl_chart"])
        with ui.element("div").classes("mc-span-4"):
            _render_btc_feed(data["btc_feed"])
    with ui.element("div").classes("mc-dashboard-grid mc-balanced-grid"):
        with ui.element("div").classes("mc-span-3"):
            _render_active_bet(data["active_bet"])
        with ui.element("div").classes("mc-span-3"):
            _render_risk(data["risk"])
        with ui.element("div").classes("mc-span-3"):
            _render_wallet_alpha(data)
        with ui.element("div").classes("mc-span-3"):
            _render_strategy_performance(data)


def _render_paper_portfolio(data: dict[str, Any]) -> None:
    from nicegui import ui

    portfolio = data.get("paper_portfolio") or {}
    curve = portfolio.get("equity_curve") or {}
    points = _line_points(curve.get("points") or [], "equity")
    with ui.element("div").classes("mc-card mc-chart-card"):
        ui.html('<div class="mc-card-title">Paper Portfolio — Equity Curve</div>', sanitize=False)
        _recharts_line("paper-equity", points, "equity", "#57d68d", height=300)
    with ui.element("div").classes("mc-kpi-grid"):
        for label_text, value, css in [
            ("Cash", f'${float(portfolio.get("cash") or 0):.2f}', ""),
            ("Exposure", f'${float(portfolio.get("exposure") or 0):.2f}', ""),
            ("Today", format_pct(portfolio.get("today_return")), _pnl_class(float(portfolio.get("today_return") or 0))),
            ("Weekly", format_pct(portfolio.get("weekly_return")), _pnl_class(float(portfolio.get("weekly_return") or 0))),
            ("Monthly", format_pct(portfolio.get("monthly_return")), _pnl_class(float(portfolio.get("monthly_return") or 0))),
            ("Drawdown", format_pnl(portfolio.get("drawdown")), _pnl_class(float(portfolio.get("drawdown") or 0))),
        ]:
            _mini_card(label_text, value, css)
    def label(item: Any) -> str:
        if not isinstance(item, dict):
            return "N/A"
        return f'{item.get("label") or "unknown"} ({format_pnl(item.get("total_pnl"))})'
    with ui.element("div").classes("mc-card"):
        ui.html('<div class="mc-card-title">Attribution</div>', sanitize=False)
        _metric_rows(
            [
                ("Best Strategy", label(portfolio.get("best_strategy"))),
                ("Worst Strategy", label(portfolio.get("worst_strategy"))),
                ("Best Copied Wallet", label(portfolio.get("best_wallet"))),
                ("Worst Copied Wallet", label(portfolio.get("worst_wallet"))),
                ("Maximum Drawdown", format_pnl(portfolio.get("max_drawdown"))),
            ]
        )


def _render_outcome_validation(data: dict[str, Any]) -> None:
    from nicegui import ui

    validation = data.get("outcome_validation") or {}
    with ui.element("div").classes("mc-kpi-grid"):
        for label_text, value, css in [
            ("Resolved Today", str(int(validation.get("todays_resolved_markets") or 0)), ""),
            ("Accuracy", format_pct(validation.get("validation_accuracy")), _pnl_class(float(validation.get("validation_accuracy") or 0))),
            ("Brier Score", f'{float(validation.get("brier_score") or 0):.3f}', ""),
            ("Confidence Bias", format_pct(validation.get("confidence_bias")), _pnl_class(-abs(float(validation.get("confidence_bias") or 0)))),
            ("Calibration Drift", format_pct(validation.get("calibration_drift")), _pnl_class(-abs(float(validation.get("calibration_drift") or 0)))),
        ]:
            _mini_card(label_text, value, css)

    def ranked_rows(items: list[dict[str, Any]]) -> str:
        if not items:
            return "N/A"
        return ", ".join(
            f'{item.get("label") or "unknown"} {format_pct(item.get("accuracy"))}'
            for item in items[:3]
        )

    with ui.element("div").classes("mc-grid-2"):
        with ui.element("div").classes("mc-card"):
            ui.html('<div class="mc-card-title">Outcome Validation</div>', sanitize=False)
            _metric_rows(
                [
                    ("Best Wallets", ranked_rows(validation.get("best_wallets") or [])),
                    ("Worst Wallets", ranked_rows(validation.get("worst_wallets") or [])),
                    ("Best Strategies", ranked_rows(validation.get("best_strategies") or [])),
                    ("Brier Score", f'{float(validation.get("brier_score") or 0):.3f}'),
                ]
            )
        with ui.element("div").classes("mc-card"):
            ui.html('<div class="mc-card-title">Newly Resolved Markets</div>', sanitize=False)
            rows = validation.get("newly_resolved_markets") or []
            if not rows:
                ui.html('<div class="mc-empty">No resolved markets validated yet</div>', sanitize=False)
            else:
                body = "".join(
                    f'<div class="mc-row"><span>{_html(str(row.get("market_title") or "market")[:72])}</span>'
                    f'<strong>{_html(str(row.get("status") or "unknown"))}</strong></div>'
                    for row in rows[:6]
                )
                ui.html(body, sanitize=False)


def _render_wallet_alpha(data: dict[str, Any]) -> None:
    from nicegui import ui

    kpis = data["kpis"]
    with ui.element("div").classes("mc-card mc-fill-card"):
        ui.html('<div class="mc-card-title">Wallet Alpha</div>', sanitize=False)
        _metric_rows(
            [
                ("Wallet", _truncate(data["header"].get("wallet"), 28)),
                ("Bankroll", f'${float(kpis.get("bankroll") or 0):.0f}'),
                ("Free Cash", f'${float(kpis.get("available_cash") or 0):.0f}'),
                ("Daily Loss Left", format_pnl(kpis.get("daily_loss_remaining"))),
            ]
        )


def _render_strategy_performance(data: dict[str, Any]) -> None:
    from nicegui import ui

    kpis = data["kpis"]
    header = data["header"]
    streak = kpis.get("current_streak") or {"type": "none", "count": 0}
    streak_label = "None" if streak.get("type") == "none" else f'{streak["count"]} {str(streak["type"]).upper()}'
    with ui.element("div").classes("mc-card mc-fill-card"):
        ui.html('<div class="mc-card-title">Strategy Performance</div>', sanitize=False)
        _metric_rows(
            [
                ("Strategy", header.get("strategy_label")),
                ("Win Rate", format_pct(kpis.get("win_rate"))),
                ("Trades / Day", str(kpis.get("trades_per_day") if kpis.get("trades_per_day") is not None else "N/A")),
                ("Current Streak", streak_label),
            ]
        )


def _render_opportunities(data: dict[str, Any]) -> None:
    from nicegui import ui

    with ui.element("div").classes("mc-grid-2"):
        _render_active_bet(data["active_bet"])
        _render_recent_settlements(data["recent_settlements"])
    with ui.element("div").classes("mc-card"):
        ui.html('<div class="mc-card-title">Opportunity Controls</div>', sanitize=False)
        _metric_rows(
            [
                ("Execution Mode", "Paper-only monitor"),
                ("Candidate Source", "Existing Polylens paper pipeline"),
                ("Live Orders", "Disabled by dashboard"),
                ("Persistence", "Read existing stores only"),
            ]
        )


def _render_wallets(data: dict[str, Any]) -> None:
    from nicegui import ui

    kpis = data["kpis"]
    risk = data["risk"]
    with ui.element("div").classes("mc-grid-3"):
        _mini_card("Capital Deployed", data["header"].get("wallet"), "signal")
        _mini_card("Bankroll", f'${float(kpis.get("bankroll") or 0):.0f}', "")
        _mini_card("Free Cash", f'${float(kpis.get("available_cash") or 0):.0f}', "profit")
    with ui.element("div").classes("mc-card"):
        ui.html('<div class="mc-card-title">Wallet Guardrails</div>', sanitize=False)
        _metric_rows(
            [
                ("Max Stake", f'${float(risk.get("max_stake") or 0):.2f}'),
                ("Daily Loss Limit", f'${float(risk.get("daily_loss_limit") or 0):.2f}'),
                ("Daily Loss Left", format_pnl(kpis.get("daily_loss_remaining"))),
                ("Duplicate Protection", "ON" if risk.get("duplicate_suppression") else "OFF"),
            ]
        )


def _render_signals(data: dict[str, Any]) -> None:
    from nicegui import ui

    with ui.element("div").classes("mc-grid-2"):
        _render_pipeline(data["pipeline"])
        with ui.element("div").classes("mc-card"):
            ui.html('<div class="mc-card-title">Signal Quality</div>', sanitize=False)
            kpis = data["kpis"]
            streak = kpis.get("current_streak") or {"type": "none", "count": 0}
            streak_label = "None" if streak.get("type") == "none" else f'{streak["count"]} {str(streak["type"]).upper()}'
            _metric_rows(
                [
                    ("Win Rate", format_pct(kpis.get("win_rate"))),
                    ("Trades / Day", str(kpis.get("trades_per_day") if kpis.get("trades_per_day") is not None else "N/A")),
                    ("Current Streak", streak_label),
                    ("Open Trades", str(kpis.get("open_trades") or 0)),
                    ("Closed Trades", str(kpis.get("closed_trades") or 0)),
                ]
            )


def _render_strategies(data: dict[str, Any]) -> None:
    from nicegui import ui

    header = data["header"]
    risk = data["risk"]
    with ui.element("div").classes("mc-grid-2"):
        with ui.element("div").classes("mc-card"):
            ui.html('<div class="mc-card-title">Strategy Profile</div>', sanitize=False)
            _metric_rows(
                [
                    ("Active Label", header.get("strategy_label")),
                    ("Venue", header.get("venue")),
                    ("Mode", header.get("mode_label")),
                    ("Stale Data Guard", f'{float(risk.get("stale_data_guard_secs") or 0):.1f}s'),
                    ("Liquidity Guard", f'${float(risk.get("liquidity_guard_min") or 0):.2f}'),
                    ("Close Cutoff", f'{float(risk.get("close_cutoff_secs") or 0):.0f}s'),
                ]
            )
        _render_risk(data["risk"])


def _render_system_health_page(data: dict[str, Any]) -> None:
    from nicegui import ui

    with ui.element("div").classes("mc-grid-2"):
        _render_system_health(data["system_health"])
        with ui.element("div").classes("mc-card"):
            ui.html('<div class="mc-card-title">Safety Controls</div>', sanitize=False)
            risk = data["risk"]
            gate = risk.get("live_send_gate") or {}
            flags = risk.get("live_flags") or {}
            _metric_rows(
                [
                    ("Kill Switch", "ACTIVE" if risk.get("kill_switch") else "OFF"),
                    ("Kalshi Sends", "ON" if gate.get("kalshi") else "OFF"),
                    ("Polymarket Sends", "ON" if gate.get("polymarket") else "OFF"),
                    ("Live Trading Flag", "ON" if flags.get("POLYLENS_LIVE_TRADING") else "OFF"),
                    ("Autonomous Crypto", "ON" if flags.get("POLYLENS_AUTONOMOUS_CRYPTO") else "OFF"),
                    ("Risk Ack", "ON" if flags.get("POLYLENS_CONFIRM_RISK_ACK") else "OFF"),
                ]
            )


def _badge(label: str, active: bool, css_class: str) -> None:
    from nicegui import ui

    state = "active" if active else "inactive"
    ui.html(f'<span class="mc-badge {css_class} {state}">{_html(label)}</span>', sanitize=False)


def _render_kpis(kpis: dict[str, Any]) -> None:
    realized = float(kpis.get("realized_pnl") or 0.0)
    unrealized = float(kpis.get("unrealized_pnl") or 0.0)
    streak = kpis.get("current_streak") or {"type": "none", "count": 0}
    streak_label = "None" if streak.get("type") == "none" else f'{streak["count"]} {str(streak["type"]).upper()}'
    items = [
        ("Realized PnL", format_pnl(realized), _pnl_class(realized)),
        ("Unrealized PnL", format_pnl(unrealized), _pnl_class(unrealized)),
        ("Total Trades", str(kpis.get("total_trades") or 0), ""),
        ("Win Rate", format_pct(kpis.get("win_rate")), "signal"),
        ("Trades / Day", str(kpis.get("trades_per_day") if kpis.get("trades_per_day") is not None else "N/A"), ""),
        ("Streak", streak_label, _streak_class(streak)),
        ("Daily Loss Left", format_pnl(kpis.get("daily_loss_remaining")), "signal"),
        ("Available Cash", f'${float(kpis.get("available_cash") or 0):.0f}', "profit"),
    ]
    from nicegui import ui

    with ui.element("div").classes("mc-kpi-grid"):
        for label, value, css in items:
            _mini_card(label, value, css)


def _mini_card(label: Any, value: Any, css: str = "") -> None:
    from nicegui import ui

    with ui.element("div").classes("mc-card mc-kpi-card"):
        ui.html(f'<div class="label">{_html(label)}</div><div class="value {css}">{_html(value)}</div>', sanitize=False)


def _render_pnl_chart(chart: dict[str, Any]) -> None:
    from nicegui import ui

    points = _line_points(chart.get("points") or [], "pnl")
    with ui.element("div").classes("mc-card mc-chart-card"):
        ui.html('<div class="mc-card-title">24h PnL Curve</div>', sanitize=False)
        _recharts_line("pnl", points, "pnl", "#57d68d", height=320)
        ui.html(
            '<div class="mc-chart-meta">'
            f'<span>Peak <strong>{_html(format_pnl(chart.get("peak")))}</strong></span>'
            f'<span>Drawdown <strong>{_html(format_pnl(chart.get("drawdown")))}</strong></span>'
            f'<span>Current <strong>{_html(format_pnl(chart.get("current_pnl")))}</strong></span>'
            "</div>",
            sanitize=False,
        )


def _render_btc_feed(feed: dict[str, Any]) -> None:
    from nicegui import ui

    change = feed.get("change_1h_pct")
    change_text = "N/A" if change is None else f"{change:+.2f}%"
    points = _line_points(feed.get("points") or [], "price")
    with ui.element("div").classes("mc-card mc-chart-card"):
        ui.html('<div class="mc-card-title">BTC Live Spot</div>', sanitize=False)
        ui.html(
            f'<div class="mc-market-price"><span>{_html(format_price(feed.get("price")))}</span>'
            f'<small class="{_pnl_class(change if change is not None else 0.0)}">1h {change_text}</small></div>',
            sanitize=False,
        )
        _recharts_line("btc", points, "price", "#5aa7ff", height=280)
        markers = feed.get("markers") or []
        marker_text = f"{len(markers)} trade markers (1h)" if markers else "No recent entries"
        ui.html(f'<div class="mc-chart-meta"><span>{_html(marker_text)}</span></div>', sanitize=False)


def _render_pipeline(stages: list[dict[str, Any]]) -> None:
    from nicegui import ui

    with ui.element("div").classes("mc-card"):
        ui.html('<div class="mc-card-title">Signal Pipeline</div>', sanitize=False)
        with ui.element("div").classes("mc-pipeline"):
            for stage in stages:
                state = stage.get("state") or "idle"
                latency = stage.get("latency_ms")
                latency_text = f"{latency}ms" if latency is not None else "N/A"
                error = stage.get("last_error") or "None"
                ui.html(
                    f'<div class="mc-stage {state}">'
                    f'<div class="name">{_html(stage.get("name"))}</div>'
                    f'<div class="status">{_html(stage.get("status"))}</div>'
                    f'<div class="meta">Latency {latency_text}<br>{_html(stage.get("detail"))}<br>Err { _html(error) }</div>'
                    "</div>",
                    sanitize=False,
                )


def _render_active_bet(bet: dict[str, Any] | None) -> None:
    from nicegui import ui

    with ui.element("div").classes("mc-card"):
        ui.html('<div class="mc-card-title">Active Opportunity</div>', sanitize=False)
        if not bet:
            ui.html('<div class="mc-empty">No open position</div>', sanitize=False)
            return
        side = str(bet.get("side") or "").lower()
        side_class = "side-up" if side in {"up", "yes", "higher"} else "side-down"
        ui.html(
            '<div class="mc-active-bet">'
            f'<div class="market">{_html(bet.get("market_title"))}</div>'
            f'<div><span class="{side_class}">{_html(bet.get("side"))}</span> @ {float(bet.get("entry_price") or 0):.2f}</div>'
            f'<div>Size {float(bet.get("size") or 0):.2f} / Edge {float(bet.get("expected_edge") or 0):.3f}</div>'
            f'<div>Expires {_html(bet.get("expiry_countdown"))}</div>'
            f'<div>Mark {_html(format_pnl(bet.get("mark_value")))} / {_html(bet.get("strategy_label") or "N/A")}</div>'
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
        f'<td>{_html(_short_ts(row.get("settled_at")))}</td>'
        f'<td class="{css}">{_html(result.upper() or "N/A")}</td>'
        f'<td class="{_pnl_class(pnl)}">{_html(format_pnl(pnl))}</td>'
        f'<td>{_html(_truncate(row.get("market"), 28))}</td>'
        f'<td>{_html(_truncate(row.get("reason"), 20))}</td>'
        "</tr>"
    )


def _render_risk(risk: dict[str, Any]) -> None:
    from nicegui import ui

    gate = risk.get("live_send_gate") or {}
    with ui.element("div").classes("mc-card"):
        ui.html('<div class="mc-card-title">Risk Engine</div>', sanitize=False)
        _metric_rows(
            [
                ("Kill Switch", "ACTIVE" if risk.get("kill_switch") else "OFF"),
                ("Max Stake", f'${float(risk.get("max_stake") or 0):.2f}'),
                ("Daily Loss Limit", f'${float(risk.get("daily_loss_limit") or 0):.2f}'),
                ("Duplicate Suppression", "ON" if risk.get("duplicate_suppression") else "OFF"),
                ("Stale Data Guard", f'{float(risk.get("stale_data_guard_secs") or 0):.1f}s'),
                ("Liquidity Guard", f'${float(risk.get("liquidity_guard_min") or 0):.2f}'),
                ("Close Cutoff", f'{float(risk.get("close_cutoff_secs") or 0):.0f}s'),
                ("Live Send Gate", f'kalshi={gate.get("kalshi")} poly={gate.get("polymarket")}'),
            ]
        )


def _render_system_health(health: dict[str, Any]) -> None:
    from nicegui import ui

    api = health.get("api_connectivity") or {}
    rows = [
        ("Short Crypto Timer", health.get("short_crypto_timer"), _service_class(health.get("short_crypto_timer"))),
        ("Settle Timer", health.get("short_crypto_settle_timer"), _service_class(health.get("short_crypto_settle_timer"))),
        ("Dashboard Service", health.get("dashboard_service"), _service_class(health.get("dashboard_service"))),
        ("Telegram Console", health.get("telegram_console_service"), _service_class(health.get("telegram_console_service"))),
        ("TG Health Timer", health.get("telegram_console_health_timer"), _service_class(health.get("telegram_console_health_timer"))),
        ("Last Scan", health.get("last_scan_time") or "N/A", ""),
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
                    f'<div class="mc-health-row"><span>{_html(label)}</span><span class="{css}">{_html(value)}</span></div>',
                    sanitize=False,
                )


def _metric_rows(rows: list[tuple[Any, Any]]) -> None:
    from nicegui import ui

    with ui.element("div").classes("mc-metric-list"):
        for label, value in rows:
            ui.html(f'<div class="mc-metric-row"><span>{_html(label)}</span><strong>{_html(value)}</strong></div>', sanitize=False)


def _recharts_line(chart_key: str, rows: list[dict[str, Any]], value_key: str, color: str, *, height: int = 300) -> None:
    from nicegui import ui

    chart_id = f"mc-chart-{chart_key}-{abs(hash(json.dumps(rows, sort_keys=True))) % 1000000}"
    ui.html(f'<div id="{chart_id}" class="mc-rechart"></div>', sanitize=False)
    config = {"valueKey": value_key, "xKey": "label", "color": color, "height": height}
    ui.add_body_html(
        "<script>"
        f"window.PolylensRecharts.renderLine({json.dumps(chart_id)}, {json.dumps(rows)}, {json.dumps(config)});"
        "</script>",
    )


def _line_points(rows: list[dict[str, Any]], value_key: str) -> list[dict[str, Any]]:
    points: list[dict[str, Any]] = []
    for idx, row in enumerate(rows):
        label = _short_ts(row.get("ts")) if row.get("ts") else str(idx + 1)
        try:
            value = float(row.get(value_key) or 0.0)
        except (TypeError, ValueError):
            value = 0.0
        points.append({"label": label, value_key: round(value, 4)})
    return points or [{"label": "now", value_key: 0.0}]


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
    text = str(value or "N/A")
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def _html(value: Any) -> str:
    return escape(str(value if value is not None else "N/A"))
