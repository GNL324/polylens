from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from src.services.results_service import fetch_trades, summarize_results
from src.web.controls import automation_status, health_badges, prop_watch_service_card, run_allowed_command
from src.web.db import OPPORTUNITIES_DB_PATH, POLYLENS_DB_PATH, load_json, rows_or_empty
from src.web.risk import risk_payload
from src.storage.opportunities import (
    alert_metrics,
    archive_opportunity,
    active_profile_operations_metrics,
    bookmaker_pair_performance,
    book_pair_by_profile_performance,
    create_paper_trade,
    create_scanner_profile,
    create_scanner_profile_from_template,
    delete_scanner_profile,
    duplicate_scanner_profile,
    get_prop_arbitrage_opportunity,
    get_active_scanner_profile,
    latest_prop_scan_status,
    list_scanner_profiles,
    load_alert_events,
    load_prop_arbitrage_opportunities,
    load_paper_trades,
    mark_opportunity_viewed,
    operations_metrics,
    opportunity_funnel,
    opportunity_timeline,
    paper_pnl_analytics,
    paper_trade_summary,
    profile_performance,
    prop_arbitrage_summary_today,
    set_active_scanner_profile,
    settle_paper_trade,
    template_performance,
    update_scanner_profile,
)
from src.web.profile_templates import get_scanner_profile_template, templates_by_sport
from src.web.sportsbook_registry import (
    normalize_sportsbook_key,
    sportsbook_catalog as registry_sportsbook_catalog,
    sportsbook_display_name,
    sportsbook_labels as registry_sportsbook_labels,
    sportsbook_link_label,
    sportsbook_url,
)

NAV_ITEMS = ("Live Opportunities", "Results / P&L", "Operations", "Profile Performance", "Opportunity Explorer", "Scanner Config", "Alerts", "Risk Engine", "Bot Control", "Scanner Status", "Alerts / Audit")

SPORT_CATALOG: tuple[dict[str, str], ...] = (
    {"key": "basketball_nba", "label": "NBA"},
    {"key": "basketball_wnba", "label": "WNBA"},
    {"key": "baseball_mlb", "label": "MLB"},
    {"key": "americanfootball_nfl", "label": "NFL"},
    {"key": "icehockey_nhl", "label": "NHL"},
)

BASKETBALL_MARKETS: tuple[dict[str, str], ...] = (
    {"key": "player_points", "label": "Player Points"},
    {"key": "player_rebounds", "label": "Player Rebounds"},
    {"key": "player_assists", "label": "Player Assists"},
    {"key": "player_threes", "label": "Player Threes"},
    {"key": "player_blocks", "label": "Player Blocks"},
    {"key": "player_steals", "label": "Player Steals"},
    {"key": "player_turnovers", "label": "Player Turnovers"},
)

MARKET_CATALOG_BY_SPORT: dict[str, tuple[dict[str, str], ...]] = {
    "basketball_nba": BASKETBALL_MARKETS,
    "basketball_wnba": BASKETBALL_MARKETS,
    "baseball_mlb": (
        {"key": "batter_home_runs", "label": "Batter Home Runs"},
        {"key": "batter_hits", "label": "Batter Hits"},
        {"key": "batter_rbis", "label": "Batter RBIs"},
        {"key": "batter_runs_scored", "label": "Batter Runs"},
        {"key": "batter_total_bases", "label": "Batter Total Bases"},
        {"key": "pitcher_strikeouts", "label": "Pitcher Strikeouts"},
        {"key": "pitcher_outs", "label": "Pitcher Outs Recorded"},
    ),
    "americanfootball_nfl": (
        {"key": "player_pass_tds", "label": "Passing Touchdowns"},
        {"key": "player_pass_yds", "label": "Passing Yards"},
        {"key": "player_rush_yds", "label": "Rushing Yards"},
        {"key": "player_receptions", "label": "Receptions"},
        {"key": "player_reception_yds", "label": "Receiving Yards"},
        {"key": "player_anytime_td", "label": "Anytime Touchdown"},
        {"key": "player_kicking_points", "label": "Kicking Points"},
    ),
    "icehockey_nhl": (
        {"key": "player_points", "label": "Player Points"},
        {"key": "player_goals", "label": "Goals"},
        {"key": "player_assists", "label": "Assists"},
        {"key": "player_shots_on_goal", "label": "Shots on Goal"},
        {"key": "player_power_play_points", "label": "Power Play Points"},
        {"key": "goalie_saves", "label": "Goalie Saves"},
    ),
}

def should_refresh_page(page: str, ui_state: dict[str, Any]) -> bool:
    if page == "Live Opportunities" and ui_state.get("detail_dialog_open"):
        return False
    if page == "Results / P&L" and ui_state.get("settlement_dialog_open"):
        return False
    return True


def create_dashboard(db_path: str | Path = POLYLENS_DB_PATH, prop_db_path: str | Path = OPPORTUNITIES_DB_PATH) -> None:
    from nicegui import ui

    @ui.page("/")
    def command_center_page() -> None:
        ui.add_head_html(
            """
            <style>
            body { background: #f6f7f9; color: #1f2933; }
            .q-page { min-height: 100vh; }
            .polylens-wrap { max-width: 1440px; margin: 0 auto; padding: 18px; }
            .metric { border: 1px solid #d9dee7; border-radius: 6px; background: white; padding: 12px; min-width: 170px; }
            .metric .label { color: #52606d; font-size: 12px; text-transform: uppercase; }
            .metric .value { color: #102a43; font-size: 22px; font-weight: 650; }
            .status-pill { border-radius: 999px; padding: 3px 9px; font-size: 12px; background: #e8f5e9; color: #1b5e20; }
            </style>
            """
        )

        selected = {"page": NAV_ITEMS[0]}
        filters: dict[str, Any] = {"min_roi": 0.0, "sport": "", "bookmaker": "", "player": ""}
        ui_state: dict[str, Any] = {"detail_dialog_open": False, "settlement_dialog_open": False}
        content = ui.column().classes("polylens-wrap w-full gap-4")

        def render() -> None:
            if not should_refresh_page(selected["page"], ui_state):
                return
            content.clear()
            with content:
                ui.label("Polylens Command Center").classes("text-2xl font-semibold")
                ui.label("Paper/research mode only. No live betting or real-money execution is enabled.").classes("text-base font-semibold text-red-700")
                if selected["page"] == "Live Opportunities":
                    _render_live_opportunities(db_path, prop_db_path, filters, ui_state, manual_refresh, edit_active_profile)
                elif selected["page"] == "Results / P&L":
                    _render_results(db_path, prop_db_path, ui_state)
                elif selected["page"] == "Operations":
                    _render_operations(prop_db_path)
                elif selected["page"] == "Profile Performance":
                    _render_profile_performance(prop_db_path)
                elif selected["page"] == "Opportunity Explorer":
                    _render_opportunity_explorer(prop_db_path)
                elif selected["page"] == "Scanner Config":
                    _render_scanner_config(prop_db_path)
                elif selected["page"] == "Alerts":
                    _render_alerts(prop_db_path)
                elif selected["page"] == "Risk Engine":
                    _render_risk(db_path)
                elif selected["page"] == "Bot Control":
                    _render_controls()
                elif selected["page"] == "Scanner Status":
                    _render_scanner_status(prop_db_path)
                else:
                    _render_audit(db_path)

        def manual_refresh() -> None:
            if not should_refresh_page(selected["page"], ui_state):
                ui.notify("Close detail view before refreshing.")
                return
            render()

        def edit_active_profile() -> None:
            selected["page"] = "Scanner Config"
            render()

        with ui.header(elevated=True).classes("bg-white text-gray-900"):
            ui.label("Polylens").classes("font-semibold text-lg")
            for item in NAV_ITEMS:
                ui.button(item, on_click=lambda item=item: (selected.update(page=item), render())).props("flat")
            ui.space()
            ui.link("Mission Control", "/mission-control").classes("text-sm")
            ui.label("LIVE DISABLED").classes("status-pill")

        render()
        ui.timer(10.0, lambda: render() if selected["page"] == "Live Opportunities" and should_refresh_page(selected["page"], ui_state) else None)


def _render_live_opportunities(db_path: str | Path, prop_db_path: str | Path, filters: dict[str, Any], ui_state: dict[str, Any], on_refresh: Any, on_edit_profile: Any) -> None:
    from nicegui import ui

    with ui.row().classes("items-center gap-2"):
        ui.button("Refresh", icon="refresh", on_click=on_refresh).props("outline")
        ui.button("Run Scan for Active Profile", icon="play_arrow", on_click=lambda: _run_active_profile_scan(prop_db_path)).props("outline")
        ui.button("Edit Active Profile", icon="tune", on_click=on_edit_profile).props("outline")
        ui.label("Auto-refreshes every 10 seconds").classes("text-sm text-gray-600")
    _render_prop_arbitrage_section(prop_db_path, filters, ui_state)


def _render_prop_arbitrage_section(prop_db_path: str | Path, filters: dict[str, Any], ui_state: dict[str, Any]) -> None:
    from nicegui import ui

    ui.separator()
    ui.label("Prop Arbitrage Opportunities").classes("text-xl font-semibold")
    active_profile = get_active_scanner_profile(db_path=str(prop_db_path))
    _render_live_active_profile_header(active_profile)
    summary = prop_arbitrage_summary_today(str(prop_db_path))
    with ui.row().classes("gap-3"):
        _metric("Open opportunities", summary["open_opportunities"])
        _metric("Found today", summary["opportunities_found_today"])
        _metric("Highest ROI today", _percent(summary["highest_roi_today"]))
        _metric("Average ROI today", _percent(summary["average_roi_today"]))
        _metric("Potential profit today", f"{summary['potential_profit_today']:,.2f}")

    with ui.row().classes("items-end gap-3"):
        min_roi = ui.number("Minimum ROI", value=filters.get("min_roi") or 0, min=0, step=0.001).classes("w-36")
        sport = ui.input("Sport", value=filters.get("sport") or "").classes("w-44")
        bookmaker = ui.input("Bookmaker", value=filters.get("bookmaker") or "").classes("w-44")
        player = ui.input("Player search", value=filters.get("player") or "").classes("w-56")

        def apply_filters() -> None:
            filters["min_roi"] = float(min_roi.value or 0)
            filters["sport"] = str(sport.value or "").strip()
            filters["bookmaker"] = str(bookmaker.value or "").strip()
            filters["player"] = str(player.value or "").strip()
            ui.notify("Filters updated; the table will refresh automatically")

        ui.button("Apply", icon="filter_alt", on_click=apply_filters).props("outline")

    prop_rows = prop_arbitrage_rows(prop_db_path, filters, active_profile=active_profile)
    mismatch_note = profile_mismatch_note(prop_db_path, active_profile)
    if mismatch_note:
        ui.label(mismatch_note).classes("text-sm text-gray-600")
    if not active_profile:
        ui.label(live_opportunities_empty_message(active_profile, [])).classes("text-gray-600")
        return
    if not prop_rows:
        ui.label(live_opportunities_empty_message(active_profile, prop_rows)).classes("text-gray-600")
        _render_last_scan_summary(prop_db_path)
        return
    table = ui.table(
        columns=[
            {"name": "discovered_at", "label": "Discovered", "field": "discovered_at", "align": "left"},
            {"name": "player", "label": "Player", "field": "player", "align": "left"},
            {"name": "prop_type", "label": "Prop Type", "field": "prop_type", "align": "left"},
            {"name": "line", "label": "Line", "field": "line", "align": "right"},
            {"name": "over_book", "label": "Over Book", "field": "over_book", "align": "left"},
            {"name": "under_book", "label": "Under Book", "field": "under_book", "align": "left"},
            {"name": "roi_percent", "label": "ROI %", "field": "roi_percent", "align": "right"},
            {"name": "guaranteed_profit_amount", "label": "Profit", "field": "guaranteed_profit_amount", "align": "right"},
            {"name": "ranking_score", "label": "Ranking", "field": "ranking_score", "align": "right"},
            {"name": "lifecycle_status", "label": "Lifecycle", "field": "lifecycle_status", "align": "left"},
            {"name": "status", "label": "Status", "field": "status", "align": "left"},
        ],
        rows=prop_rows,
        row_key="id",
        pagination=20,
    ).classes("w-full")
    _add_sportsbook_table_slot(table, "over_book")
    _add_sportsbook_table_slot(table, "under_book")

    def handle_row_click(event: Any) -> None:
        args = event.args or []
        row = args[1] if isinstance(args, list) and len(args) > 1 else None
        if isinstance(row, dict) and row.get("id"):
            _show_prop_detail(int(row["id"]), prop_db_path, ui_state)

    table.on("rowClick", handle_row_click)


def _run_active_profile_scan(prop_db_path: str | Path) -> None:
    from nicegui import ui

    if not get_active_scanner_profile(db_path=str(prop_db_path)):
        ui.notify("No active scanner profile configured.", type="warning")
        return
    try:
        result = run_allowed_command("scan-prop-arb", timeout_seconds=180).to_dict()
        ui.notify(f"Scan complete returncode={result['returncode']}")
    except Exception as exc:
        ui.notify(str(exc), type="warning")


def _render_live_active_profile_header(active_profile: dict[str, Any] | None) -> None:
    from nicegui import ui

    ui.label("Active Profile").classes("text-lg font-semibold")
    if not active_profile:
        ui.label("No active scanner profile configured.").classes("text-sm text-red-700")
        return
        with ui.row().classes("gap-3"):
            _metric("Name", active_profile["name"])
            if active_profile.get("template_id"):
                _metric("Template", scanner_template_name(active_profile.get("template_id")) or active_profile.get("template_id"))
            _metric("Bankroll", active_profile["bankroll"])
            _metric("Min ROI", _percent(active_profile["min_roi"]))
            _metric("Interval", active_profile["interval_seconds"])
    ui.label("Sport").classes("text-sm font-semibold text-gray-700")
    _badge_row([sport_display(active_profile["sport"])])
    ui.label("Markets").classes("text-sm font-semibold text-gray-700")
    _badge_row(market_display_list(active_profile["markets"], sport=active_profile["sport"]))
    ui.label("Sportsbooks").classes("text-sm font-semibold text-gray-700")
    _sportsbook_badges(active_profile["sportsbooks"], sport=active_profile.get("sport"))


def _render_last_scan_summary(prop_db_path: str | Path) -> None:
    from nicegui import ui

    status = latest_prop_scan_status(str(prop_db_path))
    if not status:
        ui.label("No prop scan data yet").classes("text-sm text-gray-600")
        return
    raw = load_json(status.get("raw_json"), {})
    with ui.row().classes("gap-3"):
        _metric("Last scan time", status.get("started_at") or "")
        _metric("Last scan profile", raw.get("scanner_profile") or "")
        _metric("Last scan result", f"{status.get('opportunities_found') or 0} opportunities")


def _add_sportsbook_table_slot(table: Any, field: str) -> None:
    table.add_slot(
        f"body-cell-{field}",
        f"""
        <q-td :props="props">
          <a v-if="props.row.{field}_url" :href="props.row.{field}_url" target="_blank" rel="noopener noreferrer">
            {{{{ props.row.{field}_link }}}}
          </a>
          <span v-else>{{{{ props.row.{field} }}}}</span>
        </q-td>
        """,
    )


def _add_html_table_slot(table: Any, field: str, html_field: str) -> None:
    table.add_slot(
        f"body-cell-{field}",
        f"""
        <q-td :props="props">
          <span v-html="props.row.{html_field}"></span>
        </q-td>
        """,
    )


def _sportsbook_link_button(book: object, sport: object | None, *, prefix: str = "Open") -> None:
    from nicegui import ui

    label = sportsbook_display_name(book)
    url = sportsbook_url(book, sport)
    if url:
        ui.link(f"{prefix} {label}", url, new_tab=True).classes("q-btn q-btn--outline q-btn--rectangle q-btn--actionable q-focusable q-hoverable")
    else:
        ui.button(f"{prefix} {label or book}", on_click=lambda: None).props("outline disable")


def sportsbook_links_html(keys: object, *, sport: object | None = None) -> str:
    selected = deserialize_sportsbook_selection(keys)
    if not selected:
        return "All sportsbooks"
    return " ".join(_sportsbook_anchor(key, sport) for key in selected)


def book_pair_links_html(pair: object, *, sport: object | None = None) -> str:
    parts = [part.strip() for part in str(pair or "").replace(" / ", " <-> ").split("<->")]
    links = [_sportsbook_anchor(part, sport) for part in parts if part]
    return " &lt;-&gt; ".join(links)


def _sportsbook_anchor(book: object, sport: object | None = None) -> str:
    label = sportsbook_display_name(book)
    url = sportsbook_url(book, sport)
    if not url:
        return label
    return f'<a href="{url}" target="_blank" rel="noopener noreferrer">{label}</a>'


def format_bet_instructions(opportunity: dict[str, Any]) -> str:
    player = str(opportunity.get("player") or "")
    prop_type = _prop_type_label(opportunity.get("prop_type"))
    line = _line_text(opportunity.get("line"))
    over_book = sportsbook_display_name(opportunity.get("over_book"))
    under_book = sportsbook_display_name(opportunity.get("under_book"))
    return "\n".join(
        [
            f"{player} Over {line} {prop_type}",
            f"Book: {over_book}",
            f"Odds: {_american_odds(opportunity.get('over_odds'))}",
            f"Stake: ${_money(opportunity.get('stake_over'))}",
            "",
            f"{player} Under {line} {prop_type}",
            f"Book: {under_book}",
            f"Odds: {_american_odds(opportunity.get('under_odds'))}",
            f"Stake: ${_money(opportunity.get('stake_under'))}",
            "",
            f"Expected Profit: ${_money(opportunity.get('guaranteed_profit_amount'))}",
            f"ROI: {_percent(opportunity.get('guaranteed_roi'))}",
        ]
    )


def _prop_type_label(value: object) -> str:
    text = str(value or "").replace("player_", "").replace("batter_", "").replace("pitcher_", "").replace("_", " ")
    return text.title()


def _line_text(value: object) -> str:
    try:
        return f"{float(value):g}"
    except (TypeError, ValueError):
        return str(value or "")


def _american_odds(value: object) -> str:
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        return str(value or "")
    return f"+{number}" if number > 0 else str(number)


def _money(value: object) -> str:
    try:
        return f"{float(value or 0):,.2f}"
    except (TypeError, ValueError):
        return "0.00"


def _show_prop_detail(opportunity_id: int, prop_db_path: str | Path, ui_state: dict[str, Any] | None = None) -> None:
    from nicegui import ui

    ui_state = ui_state if ui_state is not None else {}
    opportunity = get_prop_arbitrage_opportunity(opportunity_id, str(prop_db_path))
    if not opportunity:
        ui.notify("Opportunity not found", type="warning")
        return
    opportunity = mark_opportunity_viewed(opportunity_id, db_path=str(prop_db_path)) or opportunity
    raw = opportunity.get("raw") or load_json(opportunity.get("raw_json"), {})
    ui_state["detail_dialog_open"] = True
    with ui.dialog() as dialog, ui.card().classes("w-[720px] max-w-full"):
        dialog.on("hide", lambda: ui_state.update(detail_dialog_open=False))
        ui.label("Prop Arbitrage Detail").classes("text-xl font-semibold")
        ui.label(f"{opportunity.get('player')} | {opportunity.get('prop_type')} | line {opportunity.get('line')}")
        ui.label(f"scan_run_id: {opportunity.get('scan_run_id')}")
        ui.label(f"Lifecycle: {opportunity.get('lifecycle_status') or 'discovered'}")
        with ui.row().classes("gap-3"):
            _metric("Over", f"{opportunity.get('over_book')} {opportunity.get('over_odds')}")
            _metric("Stake over", opportunity.get("stake_over") or 0)
            _metric("Under", f"{opportunity.get('under_book')} {opportunity.get('under_odds')}")
            _metric("Stake under", opportunity.get("stake_under") or 0)
        with ui.row().classes("gap-2"):
            _sportsbook_link_button(opportunity.get("over_book"), opportunity.get("sport"), prefix="Open")
            _sportsbook_link_button(opportunity.get("under_book"), opportunity.get("sport"), prefix="Open")
        with ui.row().classes("gap-3"):
            _metric("Implied probability", opportunity.get("implied_probability_sum") or 0)
            _metric("Guaranteed ROI", _percent(opportunity.get("guaranteed_roi")))
            _metric("Guaranteed profit", opportunity.get("guaranteed_profit_amount") or 0)
            _metric("Ranking score", opportunity.get("ranking_score") or 0)
        ui.json_editor({"content": {"opportunity": {k: v for k, v in opportunity.items() if k not in {"raw_json", "raw"}}, "raw_json": raw}}).classes("w-full h-96")
        _analytics_table("Timeline", opportunity_timeline(opportunity_id, str(prop_db_path)))

        def mark_paper() -> None:
            trade_id = create_paper_trade(opportunity_id, db_path=str(prop_db_path), notes="Marked from Command Center")
            ui.notify(f"Paper trade {trade_id} created")
            ui_state["detail_dialog_open"] = False
            dialog.close()

        def archive() -> None:
            archive_opportunity(opportunity_id, db_path=str(prop_db_path))
            ui.notify("Opportunity archived")
            ui_state["detail_dialog_open"] = False
            dialog.close()

        def copy_bet_instructions() -> None:
            text = format_bet_instructions(opportunity)
            ui.run_javascript(f"navigator.clipboard.writeText({json.dumps(text)})")
            ui.notify("Bet instructions copied")

        def close_dialog() -> None:
            ui_state["detail_dialog_open"] = False
            dialog.close()

        with ui.row().classes("gap-2"):
            ui.button("Mark as Paper Trade", icon="assignment_turned_in", on_click=mark_paper)
            ui.button("Copy Bet Instructions", icon="content_copy", on_click=copy_bet_instructions).props("outline")
            ui.button("Archive Opportunity", icon="archive", on_click=archive).props("outline")
            ui.button("Close", on_click=close_dialog).props("outline")
    dialog.open()


def _render_results(db_path: str | Path, prop_db_path: str | Path, ui_state: dict[str, Any] | None = None) -> None:
    from nicegui import ui

    summary = summarize_results(db_path)
    labels = {
        "paper_realized_pnl": "Paper Realized P&L",
        "paper_unrealized_pnl": "Paper Unrealized P&L",
        "live_realized_pnl": "Live Realized P&L",
        "live_unrealized_pnl": "Live Unrealized P&L",
        "total_volume": "Total Volume",
        "fees": "Fees",
        "net_pnl": "Net P&L",
        "win_rate": "Win Rate",
        "average_roi": "Average ROI",
        "open_positions": "Open Positions",
        "closed_trades": "Closed Trades",
    }
    with ui.row().classes("gap-3"):
        for key, label in labels.items():
            with ui.element("div").classes("metric"):
                ui.label(label).classes("label")
                ui.label(_format_metric(key, summary[key])).classes("value")
    trades = fetch_trades(db_path)
    if not trades:
        ui.label("No trades yet").classes("text-gray-600")
    else:
        ui.table(columns=[{"name": key, "label": key.replace("_", " ").title(), "field": key} for key in trades[0]], rows=trades, pagination=25).classes("w-full")
    _render_paper_prop_trades(prop_db_path, ui_state or {})


def _render_paper_prop_trades(prop_db_path: str | Path, ui_state: dict[str, Any]) -> None:
    from nicegui import ui

    ui.separator()
    ui.label("Paper Prop Trades").classes("text-xl font-semibold")
    summary = paper_trade_summary(str(prop_db_path))
    with ui.row().classes("gap-3"):
        _metric("Open paper trades", summary["open_paper_trades"])
        _metric("Settled paper trades", summary["settled_paper_trades"])
        _metric("Expected open profit", f"{summary['expected_open_profit']:,.2f}")
        _metric("Realized paper P&L", f"{summary['realized_paper_pnl']:,.2f}")
        _metric("Average expected ROI", _percent(summary["average_expected_roi"]))
        _metric("Win rate", _percent(summary["win_rate"]))
        _metric("Best trade", f"{summary['best_trade']:,.2f}")
        _metric("Worst trade", f"{summary['worst_trade']:,.2f}")

    paper_rows = paper_trade_rows(prop_db_path)
    if not paper_rows:
        ui.label("No paper prop trades yet").classes("text-gray-600")
        return
    table = ui.table(
        columns=[
            {"name": "executed_at", "label": "Executed", "field": "executed_at"},
            {"name": "player", "label": "Player", "field": "player"},
            {"name": "prop_type", "label": "Prop Type", "field": "prop_type"},
            {"name": "line", "label": "Line", "field": "line", "align": "right"},
            {"name": "books", "label": "Books", "field": "books"},
            {"name": "total_stake", "label": "Total Stake", "field": "total_stake", "align": "right"},
            {"name": "expected_profit", "label": "Expected Profit", "field": "expected_profit", "align": "right"},
            {"name": "expected_roi_percent", "label": "Expected ROI", "field": "expected_roi_percent", "align": "right"},
            {"name": "result_status", "label": "Status", "field": "result_status"},
            {"name": "realized_profit", "label": "Realized", "field": "realized_profit", "align": "right"},
            {"name": "settled_at", "label": "Settled", "field": "settled_at"},
        ],
        rows=paper_rows,
        row_key="id",
        pagination=25,
    ).classes("w-full")

    def handle_trade_click(event: Any) -> None:
        args = event.args or []
        row = args[1] if isinstance(args, list) and len(args) > 1 else None
        if isinstance(row, dict) and row.get("id"):
            _show_paper_trade_detail(int(row["id"]), prop_db_path, ui_state)

    table.on("rowClick", handle_trade_click)
    analytics = paper_pnl_analytics(str(prop_db_path))
    _analytics_table("Cumulative Paper P&L", analytics["cumulative_pnl"])
    _analytics_table("Daily Paper P&L", analytics["daily_pnl"])
    _analytics_table("Expected vs Realized Profit", analytics["expected_vs_realized"])
    _analytics_table("ROI by Bookmaker Pair", analytics["roi_by_bookmaker_pair"])


def _render_operations(prop_db_path: str | Path) -> None:
    from nicegui import ui

    ui.label("Operations").classes("text-xl font-semibold")
    metrics = operations_metrics(str(prop_db_path))
    with ui.row().classes("gap-3"):
        _metric("Last scan time", metrics.get("last_scan_time") or "")
        _metric("Scan duration", metrics.get("scan_duration") or 0)
        _metric("Scans today", metrics["scans_today"])
        _metric("Opportunities today", metrics["opportunities_today"])
        _metric("Paper trades today", metrics["paper_trades_today"])
        _metric("Settled trades today", metrics["settled_trades_today"])
        _metric("Alerts today", metrics["alerts_today"])
        _metric("Database size", metrics["database_size"])
    ui.label("Automation Status").classes("text-lg font-semibold")
    try:
        status = automation_status(str(prop_db_path))
    except Exception as exc:
        status = {"error": str(exc)}
    with ui.row().classes("gap-3"):
        for key in (
            "polylens-live-arb.service",
            "polylens-dashboard.service",
            "last_scan_time",
            "last_successful_scan",
            "last_failed_scan",
            "last_alert_sent",
            "duplicate_alerts_suppressed_today",
            "failed_alerts_today",
            "opportunities_found_today",
            "scanner_uptime",
        ):
            _metric(key.replace("_", " ").title(), status.get(key) or "")
    ui.label("Health Badges").classes("text-lg font-semibold")
    try:
        badges = health_badges(str(prop_db_path))
    except Exception as exc:
        badges = {"health": f"Failed: {exc}"}
    with ui.row().classes("gap-3"):
        for key, value in badges.items():
            _metric(key.replace("_", " ").title(), value)
    ui.label("Prop Watch Service").classes("text-lg font-semibold")
    try:
        prop_watch = prop_watch_service_card(str(prop_db_path))
    except Exception as exc:
        prop_watch = {"active": "unknown", "error_message": str(exc)}
    with ui.row().classes("gap-3"):
        _metric("Service", prop_watch.get("active") or "unknown")
        _metric("Last scan time", prop_watch.get("last_scan_time") or "")
        _metric("Last scan duration", prop_watch.get("last_scan_duration") or 0)
        _metric("Opportunities last scan", prop_watch.get("opportunities_found_last_scan") or 0)
        _metric("Alerts sent today", prop_watch.get("alerts_sent_today") or 0)
        _metric("Duplicates skipped today", prop_watch.get("duplicates_skipped_today") or 0)
        _metric("Failures today", prop_watch.get("failures_today") or 0)
    if prop_watch.get("error_message"):
        ui.label(f"Last prop-watch error: {prop_watch['error_message']}").classes("text-sm text-red-700")
    active_profile = get_active_scanner_profile(db_path=str(prop_db_path))
    ui.label("Active Scanner Profile").classes("text-lg font-semibold")
    if active_profile:
        with ui.row().classes("gap-3"):
            _metric("Name", active_profile["name"])
            if active_profile.get("template_id"):
                _metric("Template", scanner_template_name(active_profile.get("template_id")) or active_profile.get("template_id"))
            _metric("Bankroll", active_profile["bankroll"])
            _metric("Interval", active_profile["interval_seconds"])
            _metric("Min ROI", _percent(active_profile["min_roi"]))
        ui.label("Sport").classes("text-sm font-semibold text-gray-700")
        _badge_row([sport_display(active_profile["sport"])])
        ui.label("Markets").classes("text-sm font-semibold text-gray-700")
        _badge_row(market_display_list(active_profile["markets"], sport=active_profile["sport"]))
        ui.label("Sportsbooks").classes("text-sm font-semibold text-gray-700")
        _sportsbook_badges(active_profile["sportsbooks"], sport=active_profile.get("sport"))
        active_perf = active_profile_operations_metrics(str(prop_db_path))
        ui.label("Active Profile Performance").classes("text-lg font-semibold")
        with ui.row().classes("gap-3"):
            _metric("Scans today", active_perf["scans_today"])
            _metric("Opportunities today", active_perf["opportunities_today"])
            _metric("Alerts today", active_perf["alerts_today"])
            summary = active_perf.get("summary") or {}
            _metric("Avg ROI", _percent(summary.get("average_roi")))
            _metric("P&L", summary.get("realized_pnl") or 0)
    else:
        ui.label("No active scanner profile configured.").classes("text-sm text-red-700")
    alert_summary = alert_metrics(str(prop_db_path))
    with ui.row().classes("gap-3"):
        _metric("Alerts sent today", alert_summary["alerts_sent_today"])
        _metric("Duplicates suppressed today", alert_summary["duplicates_suppressed_today"])
        _metric("Failed alerts today", alert_summary["failed_alerts_today"])
        _metric("Last alert time", alert_summary.get("last_alert_time") or "")
    latest = metrics.get("latest_scan") or {}
    if latest:
        ui.label("Scanner Health").classes("text-lg font-semibold")
        with ui.row().classes("gap-3"):
            _metric("Last prop scan", latest.get("started_at") or "")
            _metric("Props fetched", latest.get("props_fetched") or 0)
            _metric("Matched pairs", latest.get("matched_pairs") or 0)
            _metric("Rejected pairs", latest.get("rejected_pairs") or 0)
            _metric("Opportunities found", latest.get("opportunities_found") or 0)
            _metric("Scan duration", latest.get("scan_duration") or 0)
    _dict_table("Rejection Reasons", metrics.get("rejection_reasons") or {})
    _analytics_table("Opportunity Funnel", opportunity_funnel(str(prop_db_path)))
    _analytics_table("Opportunity Performance by Book Pair", bookmaker_pair_performance(str(prop_db_path)))


def _render_profile_performance(prop_db_path: str | Path) -> None:
    from nicegui import ui

    ui.label("Profile Performance").classes("text-xl font-semibold")
    rows = profile_performance_rows(prop_db_path)
    if not rows:
        ui.label("No scanner profile performance data yet").classes("text-gray-600")
        return
    best_pnl = max(rows, key=lambda row: row.get("realized_pnl") or 0)
    best_roi = max(rows, key=lambda row: row.get("average_roi_value") or 0)
    most_active = max(rows, key=lambda row: row.get("scan_count") or 0)
    most_recent = max(rows, key=lambda row: str(row.get("last_scan_time") or ""))
    with ui.row().classes("gap-3"):
        _metric("Best profile by realized P&L", best_pnl.get("profile") or "")
        _metric("Best profile by average ROI", best_roi.get("profile") or "")
        _metric("Most active profile", most_active.get("profile") or "")
        _metric("Most recent active profile", most_recent.get("profile") or "")
    ui.label("Profile Performance Table").classes("text-lg font-semibold")
    table = ui.table(
        columns=[
            {"name": "profile", "label": "Profile", "field": "profile"},
            {"name": "template_name", "label": "Template", "field": "template_name"},
            {"name": "sport", "label": "Sport", "field": "sport"},
            {"name": "markets", "label": "Markets", "field": "markets"},
            {"name": "sportsbooks", "label": "Sportsbooks", "field": "sportsbooks"},
            {"name": "scan_count", "label": "Scans", "field": "scan_count"},
            {"name": "last_scan_time", "label": "Last Scan", "field": "last_scan_time"},
            {"name": "opportunities_found", "label": "Opportunities", "field": "opportunities_found"},
            {"name": "average_roi", "label": "Avg ROI", "field": "average_roi"},
            {"name": "max_roi", "label": "Max ROI", "field": "max_roi"},
            {"name": "total_expected_profit", "label": "Total Expected Profit", "field": "total_expected_profit"},
            {"name": "paper_trades_created", "label": "Paper Trades", "field": "paper_trades_created"},
            {"name": "settled_paper_trades", "label": "Settled Trades", "field": "settled_paper_trades"},
            {"name": "realized_pnl", "label": "Realized P&L", "field": "realized_pnl"},
            {"name": "alert_count", "label": "Alerts", "field": "alert_count"},
            {"name": "duplicate_alert_count", "label": "Duplicates", "field": "duplicate_alert_count"},
            {"name": "failed_alert_count", "label": "Failures", "field": "failed_alert_count"},
        ],
        rows=rows,
        pagination=20,
    ).classes("w-full")
    _add_html_table_slot(table, "sportsbooks", "sportsbooks_html")
    _analytics_table("Template Performance", template_performance_rows(prop_db_path))
    _analytics_table("Book Pair by Profile", book_pair_by_profile_rows(prop_db_path))


def _render_opportunity_explorer(prop_db_path: str | Path) -> None:
    from nicegui import ui

    ui.label("Opportunity Explorer").classes("text-xl font-semibold")
    with ui.row().classes("items-end gap-3"):
        player = ui.input("Player").classes("w-44")
        prop_type = ui.input("Prop type").classes("w-44")
        bookmaker = ui.input("Bookmaker").classes("w-44")
        lifecycle = ui.input("Lifecycle status").classes("w-44")
        min_roi = ui.number("Min ROI", value=None, step=0.001).classes("w-32")
        max_roi = ui.number("Max ROI", value=None, step=0.001).classes("w-32")
        date_from = ui.input("Date from").classes("w-40")
        date_to = ui.input("Date to").classes("w-40")
        sort_by = ui.select(["roi", "profit", "discovery_date", "ranking_score"], value="roi", label="Sort").classes("w-44")
        include_archived = ui.checkbox("Include archived", value=False)
    results = ui.column().classes("w-full")

    def search() -> None:
        results.clear()
        rows = explorer_rows(
            prop_db_path,
            {
                "player": player.value,
                "prop_type": prop_type.value,
                "bookmaker": bookmaker.value,
                "lifecycle_status": lifecycle.value,
                "min_roi": min_roi.value,
                "max_roi": max_roi.value,
                "date_from": date_from.value,
                "date_to": date_to.value,
                "sort_by": sort_by.value,
                "include_archived": include_archived.value,
            },
        )
        with results:
            if not rows:
                ui.label("No matching opportunities").classes("text-gray-600")
            else:
                ui.table(columns=[{"name": key, "label": key.replace("_", " ").title(), "field": key} for key in rows[0]], rows=rows, pagination=25).classes("w-full")

    ui.button("Search", icon="search", on_click=search).props("outline")
    search()


def _render_scanner_config(prop_db_path: str | Path) -> None:
    from nicegui import ui

    ui.label("Scanner Config").classes("text-xl font-semibold")
    ui.label("Paper/research mode only. Profiles control scanner inputs, not real-money execution.").classes("text-sm text-gray-600")
    selected_id: dict[str, int | None] = {"value": None}
    profiles_col = ui.column().classes("w-full")
    template_col = ui.column().classes("w-full gap-3")

    def create_from_template(template_id: str, activate: bool = False) -> None:
        template = get_scanner_profile_template(template_id)
        if not template:
            ui.notify("Template not found", type="warning")
            return
        errors = validate_scanner_profile_payload(template)
        if errors:
            ui.notify("; ".join(errors), type="warning")
            return
        create_scanner_profile_from_template(template, db_path=str(prop_db_path), activate=activate)
        ui.notify("Profile created" + (" and activated" if activate else ""))
        refresh_profiles()

    with template_col:
        ui.label("Template Library").classes("text-lg font-semibold")
        for sport_key, templates in templates_by_sport().items():
            ui.label(sport_display(sport_key)).classes("text-sm font-semibold text-gray-700")
            with ui.grid(columns=3).classes("w-full gap-3"):
                for template in templates:
                    with ui.card().classes("w-full"):
                        with ui.row().classes("items-center gap-2"):
                            ui.label(template["name"]).classes("font-semibold")
                            if template.get("recommended"):
                                ui.label("Recommended").classes("status-pill")
                        _badge_row([sport_display(template["sport"])])
                        _badge_row(market_display_list(template["markets"], sport=template["sport"]))
                        _sportsbook_badges(template["sportsbooks"], sport=template["sport"])
                        with ui.row().classes("gap-2"):
                            ui.label(f"Bankroll {template['bankroll']}")
                            ui.label(f"Interval {template['interval_seconds']}s")
                            ui.label(f"Min ROI {_percent(template['min_roi'])}")
                        with ui.row().classes("gap-2"):
                            ui.button("Create Profile", icon="add", on_click=lambda template_id=template["template_id"]: create_from_template(template_id)).props("outline")
                            ui.button("Create & Activate", icon="check_circle", on_click=lambda template_id=template["template_id"]: create_from_template(template_id, True)).props("outline")

    with ui.row().classes("items-end gap-3"):
        name = ui.input("Name", value="").classes("w-56")
        bankroll = ui.number("Bankroll", value=1000, min=0).classes("w-32")
        interval = ui.number("Interval seconds", value=60, min=1).classes("w-36")
        min_roi = ui.number("Min ROI", value=0, min=0, step=0.001).classes("w-32")

    ui.label("Sports").classes("text-sm font-semibold text-gray-700")
    sport_options = {sport["key"]: sport["label"] for sport in sport_catalog()}
    sport_choice = ui.radio(sport_options, value="basketball_nba").props("inline")

    ui.label("Markets").classes("text-sm font-semibold text-gray-700")
    market_checks: dict[str, Any] = {}
    market_box = ui.column().classes("w-full gap-2")

    def selected_market_keys() -> list[str]:
        return serialize_market_checkboxes({key: checkbox.value for key, checkbox in market_checks.items()}, sport=sport_choice.value)

    def render_market_checks(selected_keys: list[str] | None = None) -> None:
        selected = set(deserialize_market_selection(selected_keys or [], sport=sport_choice.value))
        market_checks.clear()
        market_box.clear()
        with market_box:
            with ui.grid(columns=4).classes("gap-x-5 gap-y-1"):
                for market in market_catalog(sport_choice.value):
                    default_checked = market["key"] == "player_points" if selected_keys is None and sport_choice.value in {"basketball_nba", "basketball_wnba"} else False
                    market_checks[market["key"]] = ui.checkbox(market["label"], value=market["key"] in selected or default_checked)

    def set_market_checks(keys: list[str]) -> None:
        render_market_checks(keys)

    def sport_changed() -> None:
        render_market_checks(selected_market_keys())

    def select_all_markets() -> None:
        for checkbox in market_checks.values():
            checkbox.value = True

    def select_no_markets() -> None:
        for checkbox in market_checks.values():
            checkbox.value = False

    with ui.row().classes("gap-2"):
        ui.button("Select All", icon="done_all", on_click=select_all_markets).props("outline")
        ui.button("Select None", icon="remove_done", on_click=select_no_markets).props("outline")
    sport_choice.on("update:model-value", lambda _: sport_changed())
    render_market_checks()

    ui.label("Sportsbooks").classes("text-sm font-semibold text-gray-700")
    ui.label("Leave every box unchecked to scan all sportsbooks.").classes("text-xs text-gray-600")
    sportsbook_checks: dict[str, Any] = {}
    with ui.grid(columns=4).classes("gap-x-5 gap-y-1"):
        for sportsbook in sportsbook_catalog():
            sportsbook_checks[sportsbook["key"]] = ui.checkbox(sportsbook["label"], value=False)

    def set_sportsbook_checks(keys: list[str]) -> None:
        selected = set(deserialize_sportsbook_selection(keys))
        for key, checkbox in sportsbook_checks.items():
            checkbox.value = key in selected

    def select_all_sportsbooks() -> None:
        for checkbox in sportsbook_checks.values():
            checkbox.value = True

    def select_no_sportsbooks() -> None:
        for checkbox in sportsbook_checks.values():
            checkbox.value = False

    with ui.row().classes("gap-2"):
        ui.button("Select All", icon="done_all", on_click=select_all_sportsbooks).props("outline")
        ui.button("Select None", icon="remove_done", on_click=select_no_sportsbooks).props("outline")

    def load_form(profile: dict[str, Any]) -> None:
        selected_id["value"] = int(profile["id"])
        name.value = profile["name"]
        sport_choice.value = deserialize_sport_selection(profile["sport"])
        set_market_checks(profile["markets"])
        set_sportsbook_checks(profile["sportsbooks"])
        bankroll.value = profile["bankroll"]
        interval.value = profile["interval_seconds"]
        min_roi.value = profile["min_roi"]

    def form_payload() -> dict[str, Any]:
        selected_markets = serialize_market_checkboxes({key: checkbox.value for key, checkbox in market_checks.items()}, sport=sport_choice.value)
        selected_sportsbooks = serialize_sportsbook_checkboxes({key: checkbox.value for key, checkbox in sportsbook_checks.items()})
        return {
            "name": str(name.value or "").strip(),
            "sport": deserialize_sport_selection(sport_choice.value),
            "markets": selected_markets,
            "sportsbooks": selected_sportsbooks,
            "bankroll": float(bankroll.value or 0),
            "interval_seconds": int(interval.value or 60),
            "min_roi": float(min_roi.value or 0),
        }

    def refresh_profiles() -> None:
        profiles_col.clear()
        rows = scanner_profile_rows(prop_db_path)
        with profiles_col:
            if not rows:
                ui.label("No scanner profiles yet").classes("text-gray-600")
                return
            table = ui.table(
                columns=[{"name": key, "label": key.replace("_", " ").title(), "field": key} for key in rows[0]],
                rows=rows,
                row_key="id",
                pagination=10,
            ).classes("w-full")

            def row_click(event: Any) -> None:
                args = event.args or []
                row = args[1] if isinstance(args, list) and len(args) > 1 else None
                if isinstance(row, dict):
                    profile = next((item for item in list_scanner_profiles(db_path=str(prop_db_path)) if int(item["id"]) == int(row["id"])), None)
                    if profile:
                        load_form(profile)

            table.on("rowClick", row_click)

    def create_profile() -> None:
        payload = form_payload()
        errors = validate_scanner_profile_payload(payload)
        if errors:
            ui.notify("; ".join(errors), type="warning")
            return
        create_scanner_profile(db_path=str(prop_db_path), **payload)
        ui.notify("Profile created")
        refresh_profiles()

    def update_profile() -> None:
        if selected_id["value"] is None:
            ui.notify("Select a profile first", type="warning")
            return
        payload = form_payload()
        errors = validate_scanner_profile_payload(payload)
        if errors:
            ui.notify("; ".join(errors), type="warning")
            return
        update_scanner_profile(selected_id["value"], db_path=str(prop_db_path), **payload)
        ui.notify("Profile updated")
        refresh_profiles()

    def set_active() -> None:
        if selected_id["value"] is None:
            ui.notify("Select a profile first", type="warning")
            return
        set_active_scanner_profile(selected_id["value"], db_path=str(prop_db_path))
        ui.notify("Active profile updated")
        refresh_profiles()

    def delete_profile() -> None:
        if selected_id["value"] is None:
            ui.notify("Select a profile first", type="warning")
            return
        delete_scanner_profile(selected_id["value"], db_path=str(prop_db_path))
        selected_id["value"] = None
        ui.notify("Profile deleted")
        refresh_profiles()

    def duplicate_profile() -> None:
        if selected_id["value"] is None:
            ui.notify("Select a profile first", type="warning")
            return
        duplicate_scanner_profile(selected_id["value"], db_path=str(prop_db_path))
        ui.notify("Profile duplicated")
        refresh_profiles()

    def run_scan() -> None:
        profile = get_active_scanner_profile(db_path=str(prop_db_path))
        if not profile:
            ui.notify("No active scanner profile configured.", type="warning")
            return
        try:
            result = run_allowed_command("scan-prop-arb", timeout_seconds=180).to_dict()
            ui.notify(f"Scan complete returncode={result['returncode']}")
        except Exception as exc:
            ui.notify(str(exc), type="warning")

    with ui.row().classes("gap-2"):
        ui.button("Create Profile", icon="add", on_click=create_profile)
        ui.button("Update Profile", icon="save", on_click=update_profile).props("outline")
        ui.button("Set Active", icon="check_circle", on_click=set_active).props("outline")
        ui.button("Duplicate Profile", icon="content_copy", on_click=duplicate_profile).props("outline")
        ui.button("Delete Profile", icon="delete", on_click=delete_profile).props("outline")
        ui.button("Run Scan With Active Profile", icon="play_arrow", on_click=run_scan).props("outline")
    refresh_profiles()


def _show_paper_trade_detail(trade_id: int, prop_db_path: str | Path, ui_state: dict[str, Any] | None = None) -> None:
    from nicegui import ui

    ui_state = ui_state if ui_state is not None else {}
    trade = next((row for row in load_paper_trades(db_path=str(prop_db_path), limit=500) if int(row["id"]) == trade_id), None)
    if not trade:
        ui.notify("Paper trade not found", type="warning")
        return
    ui_state["settlement_dialog_open"] = True
    with ui.dialog() as dialog, ui.card().classes("w-[620px] max-w-full"):
        dialog.on("hide", lambda: ui_state.update(settlement_dialog_open=False))
        ui.label("Settle Paper Trade").classes("text-xl font-semibold")
        ui.label(f"{trade.get('player')} | {trade.get('prop_type')} | line {trade.get('line')}")
        with ui.row().classes("gap-3"):
            _metric("Over", f"{trade.get('over_book')} {trade.get('over_odds')}")
            _metric("Under", f"{trade.get('under_book')} {trade.get('under_odds')}")
            _metric("Total stake", trade.get("total_stake") or 0)
            _metric("Expected profit", trade.get("expected_profit") or 0)
        override = ui.number("Manual realized_profit override", value=None).classes("w-64")
        notes = ui.input("Settlement notes").classes("w-full")

        def settle(outcome: str) -> None:
            value = override.value
            updated = settle_paper_trade(
                trade_id,
                outcome,
                db_path=str(prop_db_path),
                realized_profit=float(value) if value not in (None, "") else None,
                notes=str(notes.value or ""),
            )
            ui.notify(f"Settled {updated['result_status']} realized={updated.get('realized_profit')}")
            ui_state["settlement_dialog_open"] = False
            dialog.close()

        def close_dialog() -> None:
            ui_state["settlement_dialog_open"] = False
            dialog.close()

        with ui.row().classes("gap-2"):
            ui.button("Settle as Over Won", icon="sports_score", on_click=lambda: settle("over-won"))
            ui.button("Settle as Under Won", icon="sports_score", on_click=lambda: settle("under-won"))
            ui.button("Settle as Push/Void", icon="remove_circle_outline", on_click=lambda: settle("push")).props("outline")
            ui.button("Close", on_click=close_dialog).props("outline")
    dialog.open()


def _render_risk(db_path: str | Path) -> None:
    from nicegui import ui

    bankroll = ui.number("Bankroll", value=1000.0, min=0).classes("w-64")
    output = ui.column().classes("w-full gap-3")

    def refresh() -> None:
        output.clear()
        payload = risk_payload(db_path, bankroll=float(bankroll.value or 0))
        with output:
            with ui.row().classes("gap-3"):
                _metric("Live Trading", "Disabled")
                _metric("Trade Allowed", "False")
                _metric("Mode", str(payload.get("risk_status", {}).get("mode", "paper")))
            _dict_table("Exposure by Platform", payload["exposure_by_platform"])
            _dict_table("Exposure by Market Type", payload["exposure_by_market_type"])
            _dict_table("Exposure by Strategy", payload["exposure_by_strategy"])
            _list_block("Stale Data Warnings", payload["stale_data_warnings"])
            _list_block("Missing Hedge Warnings", payload["missing_hedge_warnings"])

    ui.button("Refresh Risk", icon="refresh", on_click=refresh).props("outline")
    refresh()


def _render_controls() -> None:
    from nicegui import ui

    ui.label("Strict allowlist only. Commands run without shell=True.").classes("text-sm text-gray-600")
    output = ui.textarea("Command output").props("readonly autogrow").classes("w-full")

    def run(name: str) -> None:
        try:
            result = run_allowed_command(name).to_dict()
            output.value = f"$ {' '.join(result['command'])}\nreturncode={result['returncode']}\n\n{result['stdout']}\n{result['stderr']}"
        except Exception as exc:
            output.value = str(exc)

    with ui.row().classes("gap-2"):
        ui.button("Run scan-live-arb once", icon="play_arrow", on_click=lambda: run("scan-live-arb"))
        ui.button("Run scan-prop-arb once", icon="play_arrow", on_click=lambda: run("scan-prop-arb"))
        ui.button("Send Telegram Test Alert", icon="send", on_click=lambda: run("telegram-test-alert")).props("outline")
        ui.button("Systemd status", icon="terminal", on_click=lambda: run("systemd-status")).props("outline")
    with ui.row().classes("gap-2"):
        ui.button("Start Prop Watch", icon="play_arrow", on_click=lambda: run("prop-watch-start")).props("outline")
        ui.button("Stop Prop Watch", icon="stop", on_click=lambda: run("prop-watch-stop")).props("outline")
        ui.button("Restart Prop Watch", icon="restart_alt", on_click=lambda: run("prop-watch-restart")).props("outline")
        ui.button("Prop Watch Status", icon="terminal", on_click=lambda: run("prop-watch-status")).props("outline")
        ui.button("View Prop Watch Logs", icon="article", on_click=lambda: run("prop-watch-logs")).props("outline")


def _render_scanner_status(prop_db_path: str | Path) -> None:
    from nicegui import ui

    ui.label("Scanner Status").classes("text-xl font-semibold")
    status = latest_prop_scan_status(str(prop_db_path))
    if not status:
        ui.label("No prop scan data yet").classes("text-gray-600")
        return
    with ui.row().classes("gap-3"):
        _metric("Last prop scan", status.get("started_at") or "")
        _metric("Props fetched", status.get("props_fetched") or 0)
        _metric("Matched pairs", status.get("matched_pairs") or 0)
        _metric("Rejected pairs", status.get("rejected_pairs") or 0)
        _metric("Opportunities found", status.get("opportunities_found") or 0)
        _metric("Scan duration", status.get("scan_duration") or 0)
    raw = load_json(status.get("raw_json"), {})
    ui.json_editor({"content": raw}).classes("w-full h-96")


def _render_audit(db_path: str | Path) -> None:
    from nicegui import ui

    for table, sql in (
        ("scan_runs", "SELECT id, timestamp, scan_mode, candidate_count, alert_count FROM scan_runs ORDER BY id DESC LIMIT 20"),
        ("opportunities", "SELECT id, timestamp, venue_pair, raw_edge, execution_score, close_time FROM opportunities ORDER BY id DESC LIMIT 20"),
        ("alerts", "SELECT id, sent_timestamp, destination, status, error FROM alerts ORDER BY id DESC LIMIT 20"),
        ("rejected_candidates", "SELECT id, timestamp, venue_pair, rejection_reason FROM rejected_candidates ORDER BY id DESC LIMIT 20"),
    ):
        ui.label(table).classes("text-lg font-semibold")
        rows = rows_or_empty(db_path, table, sql)
        if not rows:
            ui.label("No data yet").classes("text-gray-600")
            continue
        ui.table(columns=[{"name": key, "label": key.replace("_", " ").title(), "field": key} for key in rows[0]], rows=rows, pagination=10).classes("w-full")


def _render_alerts(prop_db_path: str | Path) -> None:
    from nicegui import ui

    ui.label("Alerts").classes("text-xl font-semibold")
    metrics = alert_metrics(str(prop_db_path))
    with ui.row().classes("gap-3"):
        _metric("Alerts sent today", metrics["alerts_sent_today"])
        _metric("Duplicates suppressed today", metrics["duplicates_suppressed_today"])
        _metric("Failed alerts today", metrics["failed_alerts_today"])
        _metric("Last alert time", metrics.get("last_alert_time") or "")
        top = metrics.get("top_alerting_opportunity_today") or {}
        _metric("Top opportunity today", top.get("player") or top.get("market_title") or "")
        _metric("Average alert ROI today", _percent(metrics["average_alert_roi_today"]))
    rows = alert_rows(prop_db_path)
    if not rows:
        ui.label("No alert events yet").classes("text-gray-600")
        return
    columns = [
        {"name": "created_at", "label": "Created", "field": "created_at", "align": "left"},
        {"name": "alert_type", "label": "Type", "field": "alert_type", "align": "left"},
        {"name": "channel", "label": "Channel", "field": "channel", "align": "left"},
        {"name": "player", "label": "Player", "field": "player", "align": "left"},
        {"name": "market_title", "label": "Market Title", "field": "market_title", "align": "left"},
        {"name": "roi_percent", "label": "ROI", "field": "roi_percent", "align": "right"},
        {"name": "profit", "label": "Profit", "field": "profit", "align": "right"},
        {"name": "status", "label": "Status", "field": "status", "align": "left"},
        {"name": "reason", "label": "Reason", "field": "reason", "align": "left"},
        {"name": "raw_json", "label": "Raw JSON", "field": "raw_json", "align": "left"},
    ]
    ui.table(columns=columns, rows=rows, pagination=25).classes("w-full")


def latest_opportunities(db_path: str | Path = POLYLENS_DB_PATH, prop_db_path: str | Path = OPPORTUNITIES_DB_PATH, limit: int = 50) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for row in rows_or_empty(
        db_path,
        "opportunities",
        """
        SELECT timestamp, venue_pair, market_titles_json, raw_edge, execution_score, score_reason,
               prices_odds_json, close_time, candidate_json
        FROM opportunities ORDER BY id DESC LIMIT ?
        """,
        (limit,),
    ):
        candidate = load_json(row.get("candidate_json"), {})
        titles = load_json(row.get("market_titles_json"), {})
        rows.append(
            {
                "created_at": row.get("timestamp"),
                "platforms": row.get("venue_pair") or candidate.get("venue_pair") or "polymarket / kalshi",
                "market_type": candidate.get("market_type") or candidate.get("sport") or "event",
                "title": _title(titles, candidate),
                "roi": _percent(candidate.get("guaranteed_roi") or candidate.get("roi") or row.get("raw_edge")),
                "score_confidence": candidate.get("confidence") or row.get("execution_score") or "",
                "liquidity": candidate.get("liquidity") or candidate.get("volume") or "",
                "freshness": row.get("close_time") or candidate.get("commence_time") or "",
                "risk_notes": candidate.get("risk_notes") or row.get("score_reason") or "",
                "status": candidate.get("status") or "candidate",
            }
        )
    for row in rows_or_empty(
        prop_db_path,
        "opportunities",
        "SELECT timestamp, sport, player, market_type, guaranteed_roi, ranking_score, raw_json FROM opportunities ORDER BY timestamp DESC LIMIT ?",
        (limit,),
    ):
        raw = load_json(row.get("raw_json"), {})
        rows.append(
            {
                "created_at": row.get("timestamp"),
                "platforms": f"{raw.get('over_book', '')} / {raw.get('under_book', '')}".strip(" /") or "sportsbook",
                "market_type": row.get("market_type") or "prop",
                "title": " ".join(str(x) for x in (row.get("sport"), row.get("player"), row.get("market_type")) if x),
                "roi": _percent(row.get("guaranteed_roi")),
                "score_confidence": row.get("ranking_score") or "",
                "liquidity": raw.get("liquidity") or "",
                "freshness": row.get("timestamp"),
                "risk_notes": raw.get("risk_notes") or "",
                "status": "candidate",
            }
        )
    return sorted(rows, key=lambda item: str(item.get("created_at") or ""), reverse=True)[:limit]


def prop_arbitrage_rows(
    prop_db_path: str | Path = OPPORTUNITIES_DB_PATH,
    filters: dict[str, Any] | None = None,
    limit: int = 100,
    active_profile: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    filters = filters or {}
    rows = load_prop_arbitrage_opportunities(
        db_path=str(prop_db_path),
        min_roi=max(float(filters.get("min_roi") or 0), float((active_profile or {}).get("min_roi") or 0)) or None,
        sport=(active_profile or {}).get("sport") or str(filters.get("sport") or "").strip() or None,
        bookmaker=str(filters.get("bookmaker") or "").strip() or None,
        player=str(filters.get("player") or "").strip() or None,
        include_archived=bool(filters.get("include_archived") or False),
        limit=limit,
    )
    if active_profile:
        rows = [row for row in rows if opportunity_matches_profile(row, active_profile)]
    manual_sport = str(filters.get("sport") or "").strip()
    if active_profile and manual_sport:
        rows = [row for row in rows if row.get("sport") == manual_sport]
    for row in rows:
        row["roi_percent"] = _percent(row.get("guaranteed_roi"))
        row["over_book_link"] = sportsbook_link_label(row.get("over_book"))
        row["under_book_link"] = sportsbook_link_label(row.get("under_book"))
        row["over_book_url"] = sportsbook_url(row.get("over_book"), row.get("sport"))
        row["under_book_url"] = sportsbook_url(row.get("under_book"), row.get("sport"))
    return rows


def opportunity_matches_profile(row: dict[str, Any], profile: dict[str, Any]) -> bool:
    sport = str(profile.get("sport") or "").strip()
    if sport and row.get("sport") != sport:
        return False
    markets = set(deserialize_market_selection(profile.get("markets"), sport=sport))
    if markets and row.get("prop_type") not in markets:
        return False
    sportsbooks = set(deserialize_sportsbook_selection(profile.get("sportsbooks")))
    if not sportsbooks:
        return True
    over_book = sportsbook_key(row.get("over_book"))
    under_book = sportsbook_key(row.get("under_book"))
    return bool(over_book in sportsbooks and under_book in sportsbooks)


def profile_mismatch_count(prop_db_path: str | Path, profile: dict[str, Any]) -> int:
    rows = load_prop_arbitrage_opportunities(db_path=str(prop_db_path), include_archived=False, limit=1000)
    return sum(1 for row in rows if not opportunity_matches_profile(row, profile))


def profile_mismatch_note(prop_db_path: str | Path, profile: dict[str, Any] | None) -> str:
    if profile and profile_mismatch_count(prop_db_path, profile) > 0:
        return "Other opportunities exist outside the active profile. View them in Opportunity Explorer."
    return ""


def live_opportunities_empty_message(active_profile: dict[str, Any] | None, rows: list[dict[str, Any]]) -> str:
    if not active_profile:
        return "No active scanner profile configured. Configure one in Scanner Config."
    if not rows:
        return "No live opportunities match the active profile yet."
    return ""


def explorer_rows(prop_db_path: str | Path = OPPORTUNITIES_DB_PATH, filters: dict[str, Any] | None = None, limit: int = 100) -> list[dict[str, Any]]:
    filters = filters or {}
    rows = load_prop_arbitrage_opportunities(
        db_path=str(prop_db_path),
        player=str(filters.get("player") or "").strip() or None,
        prop_type=str(filters.get("prop_type") or "").strip() or None,
        bookmaker=str(filters.get("bookmaker") or "").strip() or None,
        lifecycle_status=str(filters.get("lifecycle_status") or "").strip() or None,
        min_roi=float(filters["min_roi"]) if filters.get("min_roi") not in (None, "") else None,
        max_roi=float(filters["max_roi"]) if filters.get("max_roi") not in (None, "") else None,
        discovered_from=str(filters.get("date_from") or "").strip() or None,
        discovered_to=str(filters.get("date_to") or "").strip() or None,
        include_archived=bool(filters.get("include_archived") or False),
        sort_by=str(filters.get("sort_by") or "roi"),
        limit=limit,
    )
    for row in rows:
        row["roi_percent"] = _percent(row.get("guaranteed_roi"))
    return rows


def paper_trade_rows(prop_db_path: str | Path = OPPORTUNITIES_DB_PATH, limit: int = 100) -> list[dict[str, Any]]:
    rows = load_paper_trades(db_path=str(prop_db_path), limit=limit)
    for row in rows:
        row["books"] = f"{row.get('over_book')} / {row.get('under_book')}"
        row["expected_roi_percent"] = _percent(row.get("expected_roi"))
    return rows


def alert_rows(prop_db_path: str | Path = OPPORTUNITIES_DB_PATH, limit: int = 100) -> list[dict[str, Any]]:
    rows = load_alert_events(db_path=str(prop_db_path), limit=limit)
    for row in rows:
        row["roi_percent"] = _percent(row.get("roi"))
    return rows


def scanner_profile_rows(prop_db_path: str | Path = OPPORTUNITIES_DB_PATH) -> list[dict[str, Any]]:
    rows = []
    performance_by_id = {row.get("profile_id"): row for row in profile_performance(str(prop_db_path))}
    for profile in list_scanner_profiles(db_path=str(prop_db_path)):
        performance = performance_by_id.get(profile["id"], {})
        rows.append(
            {
                "id": profile["id"],
                "name": profile["name"],
                "sport": sport_display(profile["sport"]),
                "markets": market_display(profile["markets"], sport=profile["sport"]),
                "sportsbooks": sportsbook_display(profile["sportsbooks"]),
                "template": scanner_template_name(profile.get("template_id")) if profile.get("template_id") else "",
                "bankroll": profile["bankroll"],
                "interval_seconds": profile["interval_seconds"],
                "min_roi": profile["min_roi"],
                "is_active": "yes" if profile["is_active"] else "no",
                "scans": performance.get("scan_count") or 0,
                "opps": performance.get("opportunities_found") or 0,
                "avg_roi": _percent(performance.get("average_roi")),
                "pnl": performance.get("realized_pnl") or 0,
            }
        )
    return rows


def profile_performance_rows(prop_db_path: str | Path = OPPORTUNITIES_DB_PATH) -> list[dict[str, Any]]:
    rows = []
    for row in profile_performance(str(prop_db_path)):
        rows.append(
            {
                **row,
                "sport": sport_display(row.get("sport")),
                "markets": market_display(row.get("markets"), sport=row.get("sport")),
                "sportsbooks": sportsbook_display(row.get("sportsbooks")),
                "sportsbooks_html": sportsbook_links_html(row.get("sportsbooks"), sport=row.get("sport")),
                "average_roi_value": row.get("average_roi") or 0,
                "average_roi": _percent(row.get("average_roi")),
                "max_roi": _percent(row.get("max_roi")),
            }
        )
    return rows


def template_performance_rows(prop_db_path: str | Path = OPPORTUNITIES_DB_PATH) -> list[dict[str, Any]]:
    rows = []
    for row in template_performance(str(prop_db_path)):
        rows.append({**row, "average_roi": _percent(row.get("average_roi"))})
    return rows


def book_pair_by_profile_rows(prop_db_path: str | Path = OPPORTUNITIES_DB_PATH) -> list[dict[str, Any]]:
    rows = []
    for row in book_pair_by_profile_performance(str(prop_db_path)):
        rows.append({**row, "average_roi": _percent(row.get("average_roi")), "book_pair_html": book_pair_links_html(row.get("book_pair"))})
    return rows


def scanner_template_name(template_id: object) -> str:
    template = get_scanner_profile_template(str(template_id or ""))
    return str(template["name"]) if template else ""


def sport_catalog() -> list[dict[str, str]]:
    return [dict(item) for item in SPORT_CATALOG]


def sport_labels() -> dict[str, str]:
    return {item["key"]: item["label"] for item in SPORT_CATALOG}


def deserialize_sport_selection(value: object) -> str:
    key = str(value or "").strip().lower()
    return key if key in sport_labels() else ""


def sport_display(key: object) -> str:
    value = deserialize_sport_selection(key)
    return sport_labels().get(value, "")


def market_catalog(sport: object = "basketball_nba") -> list[dict[str, str]]:
    sport_key = deserialize_sport_selection(sport)
    return [dict(item) for item in MARKET_CATALOG_BY_SPORT.get(sport_key, ())]


def market_labels(sport: object = "basketball_nba") -> dict[str, str]:
    return {item["key"]: item["label"] for item in market_catalog(sport)}


def deserialize_market_selection(value: object, *, sport: object = "basketball_nba") -> list[str]:
    if value in (None, ""):
        return []
    if isinstance(value, str):
        candidates = value.split(",")
    elif isinstance(value, (list, tuple, set)):
        candidates = list(value)
    else:
        candidates = [value]
    known = set(market_labels(sport))
    selected: list[str] = []
    for candidate in candidates:
        key = str(candidate or "").strip().lower()
        if not key or key not in known or key in selected:
            continue
        selected.append(key)
    return selected


def serialize_market_checkboxes(values: dict[str, object], *, sport: object = "basketball_nba") -> list[str]:
    known_order = [item["key"] for item in market_catalog(sport)]
    return [key for key in known_order if bool(values.get(key))]


def market_display(keys: object, *, sport: object = "basketball_nba") -> str:
    labels = market_labels(sport)
    return ", ".join(labels.get(key, key) for key in deserialize_market_selection(keys, sport=sport))


def market_display_list(keys: object, *, sport: object = "basketball_nba") -> list[str]:
    labels = market_labels(sport)
    return [labels.get(key, key) for key in deserialize_market_selection(keys, sport=sport)]


def markets_after_sport_change(markets: object, new_sport: object) -> list[str]:
    return deserialize_market_selection(markets, sport=new_sport)


def validate_scanner_profile_payload(payload: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if not str(payload.get("name") or "").strip():
        errors.append("Name is required")
    if not deserialize_sport_selection(payload.get("sport")):
        errors.append("Select one sport")
    sport = deserialize_sport_selection(payload.get("sport"))
    markets = deserialize_market_selection(payload.get("markets"), sport=sport)
    if not markets:
        errors.append("Select at least one market")
    invalid_markets = [key for key in _raw_list(payload.get("markets")) if key not in set(market_labels(sport))]
    if sport and invalid_markets:
        errors.append(f"Selected markets are not valid for {sport_display(sport)}: {', '.join(invalid_markets)}")
    return errors


def _raw_list(value: object) -> list[str]:
    if value in (None, ""):
        return []
    if isinstance(value, str):
        candidates = value.split(",")
    elif isinstance(value, (list, tuple, set)):
        candidates = list(value)
    else:
        candidates = [value]
    result: list[str] = []
    for candidate in candidates:
        key = str(candidate or "").strip().lower()
        if key and key not in result:
            result.append(key)
    return result


def sportsbook_catalog() -> list[dict[str, str]]:
    return registry_sportsbook_catalog()


def sportsbook_labels() -> dict[str, str]:
    return registry_sportsbook_labels()


def sportsbook_key(value: object) -> str:
    return normalize_sportsbook_key(value)


def deserialize_sportsbook_selection(value: object) -> list[str]:
    if value in (None, ""):
        return []
    if isinstance(value, str):
        candidates = value.split(",")
    elif isinstance(value, (list, tuple, set)):
        candidates = list(value)
    else:
        candidates = [value]
    known = set(sportsbook_labels())
    selected: list[str] = []
    for candidate in candidates:
        key = sportsbook_key(candidate)
        if not key or key not in known or key in selected:
            continue
        selected.append(key)
    return selected


def serialize_sportsbook_checkboxes(values: dict[str, object]) -> list[str]:
    known_order = [item["key"] for item in sportsbook_catalog()]
    return [key for key in known_order if bool(values.get(key))]


def sportsbook_display(keys: object, *, all_label: str = "All sportsbooks") -> str:
    selected = deserialize_sportsbook_selection(keys)
    if not selected:
        return all_label
    return ", ".join(sportsbook_display_name(key) for key in selected)


def _sportsbook_badges(keys: object, *, sport: object | None = None) -> None:
    from nicegui import ui

    selected = deserialize_sportsbook_selection(keys)
    if not selected:
        _badge_row(["All sportsbooks"])
        return
    with ui.row().classes("gap-2"):
        for key in selected:
            url = sportsbook_url(key, sport)
            label = sportsbook_display_name(key)
            if url:
                ui.link(label, url, new_tab=True).classes("status-pill no-underline")
            else:
                ui.label(label).classes("status-pill")


def _badge_row(labels: list[str]) -> None:
    from nicegui import ui

    with ui.row().classes("gap-2"):
        for label in labels:
            ui.label(label).classes("status-pill")


def _metric(label: str, value: object) -> None:
    from nicegui import ui

    with ui.element("div").classes("metric"):
        ui.label(label).classes("label")
        ui.label(str(value)).classes("value")


def _dict_table(title: str, values: dict[str, Any]) -> None:
    from nicegui import ui

    ui.label(title).classes("text-lg font-semibold")
    if not values:
        ui.label("No data yet").classes("text-gray-600")
        return
    ui.table(columns=[{"name": "name", "label": "Name", "field": "name"}, {"name": "value", "label": "Exposure", "field": "value"}], rows=[{"name": k, "value": v} for k, v in values.items()]).classes("w-full")


def _list_block(title: str, values: list[str]) -> None:
    from nicegui import ui

    ui.label(title).classes("text-lg font-semibold")
    if not values:
        ui.label("No warnings").classes("text-gray-600")
        return
    for value in values:
        ui.label(value)


def _analytics_table(title: str, rows: list[dict[str, Any]]) -> None:
    from nicegui import ui

    ui.label(title).classes("text-lg font-semibold")
    if not rows:
        ui.label("No data yet").classes("text-gray-600")
        return
    table = ui.table(columns=[{"name": key, "label": key.replace("_", " ").title(), "field": key} for key in rows[0] if not key.endswith("_html")], rows=rows, pagination=10).classes("w-full")
    if rows and "book_pair_html" in rows[0]:
        _add_html_table_slot(table, "book_pair", "book_pair_html")


def _format_metric(key: str, value: object) -> str:
    if key in {"win_rate", "average_roi"}:
        return _percent(value)
    if isinstance(value, float):
        return f"{value:,.2f}"
    return str(value)


def _percent(value: object) -> str:
    try:
        number = float(value or 0)
    except (TypeError, ValueError):
        return ""
    return f"{number * 100:.2f}%" if abs(number) <= 1 else f"{number:.2f}%"


def _title(titles: dict[str, Any], candidate: dict[str, Any]) -> str:
    for key in ("title", "market_title", "polymarket_title", "kalshi_title"):
        if candidate.get(key):
            return str(candidate[key])
    return " / ".join(str(value) for value in titles.values() if value) or "Untitled opportunity"
