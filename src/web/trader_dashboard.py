from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.analysis.trader_alpha import build_trader_alpha_report, rank_trader_alpha
from src.analysis.trader_discovery import (
    DEFAULT_TRADER_DISCOVERY_DB,
    discovery_report,
    load_discovered_wallets,
)
from src.analysis.trader_insights import build_trader_insight_report
from src.analysis.trader_network import build_trader_network, network_summary
from src.analysis.trader_profiler import derive_specialization, profile_traders, profile_wallet
from src.analysis.trader_registry import DEFAULT_TRADERS_DB, registry_stats
from src.analysis.trader_scanner import DEFAULT_WALLET_EXPORT_DIR
from src.analysis.wallet_activity import validate_wallet
from src.sqlite_utils import closing_connection

DEFAULT_TRADER_DASHBOARD_HOST = "127.0.0.1"
DEFAULT_TRADER_DASHBOARD_PORT = 8788
DEFAULT_LEADERBOARD_LIMIT = 25
DEFAULT_NETWORK_LIMIT = 50

TRADER_NAV_ITEMS = (
    "Home",
    "Wallet Search",
    "Alpha Leaderboard",
    "Connected Traders",
    "Insight Reports",
    "Network Explorer",
)

TRADER_DASHBOARD_CSS = """
body { background: #0f172a; color: #e2e8f0; }
.trader-shell { max-width: 1400px; margin: 0 auto; padding: 1.25rem; }
.trader-header { display: flex; align-items: center; justify-content: space-between; gap: 1rem; margin-bottom: 1rem; }
.trader-title { font-size: 1.75rem; font-weight: 700; color: #f8fafc; }
.trader-subtitle { color: #94a3b8; font-size: 0.95rem; }
.trader-pill { background: #1e293b; border: 1px solid #334155; color: #cbd5e1; padding: 0.2rem 0.65rem; border-radius: 999px; font-size: 0.75rem; }
.trader-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 0.85rem; }
.trader-card { background: #111827; border: 1px solid #334155; border-radius: 0.85rem; padding: 1rem; }
.trader-card .label { color: #94a3b8; font-size: 0.8rem; text-transform: uppercase; letter-spacing: 0.04em; }
.trader-card .value { color: #f8fafc; font-size: 1.6rem; font-weight: 700; margin-top: 0.35rem; }
.trader-section-title { font-size: 1.15rem; font-weight: 600; color: #f8fafc; margin: 1rem 0 0.5rem; }
.trader-panel { background: #111827; border: 1px solid #334155; border-radius: 0.85rem; padding: 1rem; }
.trader-actions { display: flex; flex-wrap: wrap; gap: 0.5rem; margin: 0.75rem 0; }
"""


@dataclass
class TraderDashboardConfig:
    traders_db_path: str | Path = DEFAULT_TRADERS_DB
    discovery_db_path: str | Path = DEFAULT_TRADER_DISCOVERY_DB
    wallet_export_dir: str | Path = DEFAULT_WALLET_EXPORT_DIR
    seed_wallet: str = ""
    leaderboard_limit: int = DEFAULT_LEADERBOARD_LIMIT
    network_limit: int = DEFAULT_NETWORK_LIMIT


def trader_home_summary(
    *,
    seed_wallet: str | None = None,
    traders_db_path: str | Path = DEFAULT_TRADERS_DB,
    discovery_db_path: str | Path = DEFAULT_TRADER_DISCOVERY_DB,
    wallet_export_dir: str | Path = DEFAULT_WALLET_EXPORT_DIR,
) -> dict[str, Any]:
    seed_wallet = _normalize_wallet(seed_wallet) or None
    network = build_trader_network(
        wallet=seed_wallet,
        traders_db_path=traders_db_path,
        discovery_db_path=discovery_db_path,
        wallet_export_dir=wallet_export_dir,
    )
    registry = registry_stats(db_path=str(traders_db_path))
    return {
        "wallets_discovered": _discovery_wallet_count(discovery_db_path),
        "network_nodes": len(network.nodes),
        "network_edges": len(network.edges),
        "clusters": network.clusters,
        "profiles_generated": int(registry.get("total_traders") or 0),
        "seed_wallet": seed_wallet or "",
    }


def load_alpha_leaderboard(
    *,
    limit: int = DEFAULT_LEADERBOARD_LIMIT,
    traders_db_path: str | Path = DEFAULT_TRADERS_DB,
    discovery_db_path: str | Path = DEFAULT_TRADER_DISCOVERY_DB,
) -> list[dict[str, Any]]:
    ranked = rank_trader_alpha(
        limit=limit,
        traders_db_path=traders_db_path,
        discovery_db_path=discovery_db_path,
    )
    rows: list[dict[str, Any]] = []
    for item in ranked:
        profile = _profile_label_from_alpha_row(item)
        rows.append(
            {
                "wallet": item["wallet"],
                "alpha": item["alpha_score"],
                "watch_score": item["watch_score"],
                "profile": profile,
                "classification": item["classification"],
            }
        )
    rows.sort(key=lambda row: (-row["alpha"], -row["watch_score"], row["wallet"]))
    return rows


def load_connected_traders(
    *,
    seed_wallet: str | None = None,
    limit: int = DEFAULT_LEADERBOARD_LIMIT,
    traders_db_path: str | Path = DEFAULT_TRADERS_DB,
    discovery_db_path: str | Path = DEFAULT_TRADER_DISCOVERY_DB,
    wallet_export_dir: str | Path = DEFAULT_WALLET_EXPORT_DIR,
) -> list[dict[str, Any]]:
    seed_wallet = _normalize_wallet(seed_wallet) or None
    network = build_trader_network(
        wallet=seed_wallet,
        traders_db_path=traders_db_path,
        discovery_db_path=discovery_db_path,
        wallet_export_dir=wallet_export_dir,
    )
    summary = network_summary(network, limit=limit, focus_wallet=seed_wallet)
    rows: list[dict[str, Any]] = []
    for item in summary["top_connected_wallets"]:
        profile = _profile_label_for_wallet(
            item["wallet"],
            traders_db_path=traders_db_path,
            discovery_db_path=discovery_db_path,
        )
        rows.append(
            {
                "wallet": item["wallet"],
                "degree": item.get("degree", 0),
                "weighted_degree": item.get("weighted_degree", 0.0),
                "shared_markets": profile["shared_markets"],
                "profile": profile["profile"],
            }
        )
    return rows


def load_network_explorer_rows(
    *,
    seed_wallet: str | None = None,
    limit: int = DEFAULT_NETWORK_LIMIT,
    traders_db_path: str | Path = DEFAULT_TRADERS_DB,
    discovery_db_path: str | Path = DEFAULT_TRADER_DISCOVERY_DB,
    wallet_export_dir: str | Path = DEFAULT_WALLET_EXPORT_DIR,
) -> list[dict[str, Any]]:
    seed_wallet = _normalize_wallet(seed_wallet) or None
    network = build_trader_network(
        wallet=seed_wallet,
        traders_db_path=traders_db_path,
        discovery_db_path=discovery_db_path,
        wallet_export_dir=wallet_export_dir,
    )
    ordered = sorted(
        network.nodes.values(),
        key=lambda node: (-node.weighted_degree, -node.centrality_score, -node.alpha_score, node.wallet),
    )
    return [
        {
            "wallet": node.wallet,
            "degree": node.degree,
            "centrality": round(node.centrality_score, 4),
            "cluster": node.cluster_id,
            "alpha": node.alpha_score,
        }
        for node in ordered[: max(int(limit or DEFAULT_NETWORK_LIMIT), 0)]
    ]


def analyze_wallet_details(
    wallet: str,
    *,
    scan_if_missing: bool = True,
    traders_db_path: str | Path = DEFAULT_TRADERS_DB,
    discovery_db_path: str | Path = DEFAULT_TRADER_DISCOVERY_DB,
    wallet_export_dir: str | Path = DEFAULT_WALLET_EXPORT_DIR,
) -> dict[str, Any]:
    wallet = _normalize_wallet(wallet)
    validate_wallet(wallet)
    profile = profile_wallet(
        wallet,
        scan_if_missing=scan_if_missing,
        traders_db_path=traders_db_path,
        discovery_db_path=discovery_db_path,
        wallet_export_dir=wallet_export_dir,
    )
    return {
        "wallet": profile.wallet,
        "classification": profile.classification,
        "confidence": profile.confidence,
        "watch_score": profile.watch_score,
        "alpha_score": profile.alpha_score,
        "profile": profile.profile,
        "markets_traded": profile.markets_traded,
        "shared_markets": profile.shared_markets,
        "btc_volume": profile.btc_volume,
        "eth_volume": profile.eth_volume,
        "sol_volume": profile.sol_volume,
        "merge_count": profile.merge_count,
        "redeem_count": profile.redeem_count,
    }


def generate_insight_report(
    wallet: str,
    *,
    limit: int = DEFAULT_LEADERBOARD_LIMIT,
    traders_db_path: str | Path = DEFAULT_TRADERS_DB,
    discovery_db_path: str | Path = DEFAULT_TRADER_DISCOVERY_DB,
    wallet_export_dir: str | Path = DEFAULT_WALLET_EXPORT_DIR,
) -> dict[str, Any]:
    wallet = _normalize_wallet(wallet)
    validate_wallet(wallet)
    return build_trader_insight_report(
        wallet,
        limit=limit,
        scan_if_missing=False,
        traders_db_path=traders_db_path,
        discovery_db_path=discovery_db_path,
        wallet_export_dir=wallet_export_dir,
    )


def run_discovery_action(
    wallet: str | None = None,
    *,
    limit: int = DEFAULT_LEADERBOARD_LIMIT,
    traders_db_path: str | Path = DEFAULT_TRADERS_DB,
    discovery_db_path: str | Path = DEFAULT_TRADER_DISCOVERY_DB,
) -> dict[str, Any]:
    wallet = _normalize_wallet(wallet) or None
    if wallet:
        validate_wallet(wallet)
    return discovery_report(
        wallet=wallet,
        registry_db_path=traders_db_path,
        discovery_db_path=discovery_db_path,
        limit=limit,
        scan=False,
    )


def run_profiling_action(
    *,
    seed_wallet: str | None = None,
    limit: int = DEFAULT_LEADERBOARD_LIMIT,
    traders_db_path: str | Path = DEFAULT_TRADERS_DB,
    discovery_db_path: str | Path = DEFAULT_TRADER_DISCOVERY_DB,
    wallet_export_dir: str | Path = DEFAULT_WALLET_EXPORT_DIR,
) -> dict[str, Any]:
    seed_wallet = _normalize_wallet(seed_wallet) or None
    profiles = profile_traders(
        top_connected=bool(seed_wallet),
        focus_wallet=seed_wallet,
        limit=limit,
        scan_if_missing=False,
        traders_db_path=traders_db_path,
        discovery_db_path=discovery_db_path,
        wallet_export_dir=wallet_export_dir,
    )
    if not profiles and seed_wallet is None:
        discovered = load_discovered_wallets(discovery_db_path, limit=limit)
        profiles = profile_traders(
            wallets=[candidate.wallet for candidate in discovered],
            limit=limit,
            scan_if_missing=False,
            traders_db_path=traders_db_path,
            discovery_db_path=discovery_db_path,
            wallet_export_dir=wallet_export_dir,
        )
    return {"profiles_generated": len(profiles), "profiles": profiles}


def refresh_insights_action(
    wallet: str,
    *,
    limit: int = DEFAULT_LEADERBOARD_LIMIT,
    traders_db_path: str | Path = DEFAULT_TRADERS_DB,
    discovery_db_path: str | Path = DEFAULT_TRADER_DISCOVERY_DB,
    wallet_export_dir: str | Path = DEFAULT_WALLET_EXPORT_DIR,
) -> dict[str, Any]:
    report = generate_insight_report(
        wallet,
        limit=limit,
        traders_db_path=traders_db_path,
        discovery_db_path=discovery_db_path,
        wallet_export_dir=wallet_export_dir,
    )
    return {
        "seed_wallet": report["seed_wallet"],
        "summary": report["summary"],
        "recommended_traders": report["recommended_traders"],
    }


def create_trader_dashboard(config: TraderDashboardConfig | None = None) -> None:
    from nicegui import ui

    settings = config or TraderDashboardConfig()
    state: dict[str, Any] = {
        "page": "Home",
        "seed_wallet": settings.seed_wallet,
        "wallet_search": settings.seed_wallet,
        "insight_wallet": settings.seed_wallet,
        "last_action": "",
        "wallet_details": None,
        "insight_report": None,
    }

    @ui.page("/")
    def trader_dashboard_page() -> None:
        ui.add_head_html(f"<style>{TRADER_DASHBOARD_CSS}</style>")
        shell = ui.column().classes("trader-shell w-full gap-3")
        header = ui.row().classes("trader-header w-full")
        nav = ui.row().classes("gap-2 flex-wrap")
        content = ui.column().classes("w-full gap-3")

        def set_page(page: str) -> None:
            state["page"] = page
            render()

        def render() -> None:
            content.clear()
            with content:
                page = state["page"]
                if page == "Home":
                    _render_home_page(state, settings, set_page)
                elif page == "Wallet Search":
                    _render_wallet_search_page(state, settings)
                elif page == "Alpha Leaderboard":
                    _render_alpha_leaderboard_page(settings)
                elif page == "Connected Traders":
                    _render_connected_traders_page(state, settings)
                elif page == "Insight Reports":
                    _render_insight_reports_page(state, settings)
                elif page == "Network Explorer":
                    _render_network_explorer_page(state, settings)

        with header:
            with ui.column().classes("gap-1"):
                ui.label("Trader Intelligence Center").classes("trader-title")
                ui.label("Read-only research dashboard for discovery, network, alpha, and profiling.").classes("trader-subtitle")
            ui.label("RESEARCH ONLY").classes("trader-pill")

        with nav:
            for item in TRADER_NAV_ITEMS:
                ui.button(
                    item,
                    on_click=lambda item=item: set_page(item),
                ).props("flat" if state["page"] != item else "unelevated color=primary")

        with ui.row().classes("trader-actions w-full"):
            ui.input("Seed Wallet", value=state["seed_wallet"]).bind_value(state, "seed_wallet").classes("w-[420px]")
            ui.button("Run Discovery", icon="travel_explore", on_click=lambda: _handle_discovery(state, settings)).props("outline")
            ui.button("Run Profiling", icon="badge", on_click=lambda: _handle_profiling(state, settings)).props("outline")
            ui.button("Refresh Insights", icon="insights", on_click=lambda: _handle_refresh_insights(state, settings)).props("outline")
            ui.button("Refresh View", icon="refresh", on_click=render).props("outline")
        if state.get("last_action"):
            ui.label(state["last_action"]).classes("text-sm text-slate-300")

        render()

    _ = trader_dashboard_page


def run_trader_dashboard(
    host: str | None = None,
    port: int | None = None,
    config: TraderDashboardConfig | None = None,
) -> None:
    from nicegui import ui

    bind_host = host or DEFAULT_TRADER_DASHBOARD_HOST
    bind_port = int(port or DEFAULT_TRADER_DASHBOARD_PORT)
    create_trader_dashboard(config=config)
    ui.run(host=bind_host, port=bind_port, title="Trader Intelligence Center", reload=False, show=False)


def _render_home_page(state: dict[str, Any], settings: TraderDashboardConfig, set_page) -> None:
    from nicegui import ui

    summary = trader_home_summary(
        seed_wallet=state.get("seed_wallet") or None,
        traders_db_path=settings.traders_db_path,
        discovery_db_path=settings.discovery_db_path,
        wallet_export_dir=settings.wallet_export_dir,
    )
    ui.label("Overview").classes("trader-section-title")
    with ui.element("div").classes("trader-grid w-full"):
        for key, label in (
            ("wallets_discovered", "Wallets Discovered"),
            ("network_nodes", "Network Nodes"),
            ("network_edges", "Network Edges"),
            ("clusters", "Clusters"),
            ("profiles_generated", "Profiles Generated"),
        ):
            with ui.element("div").classes("trader-card"):
                ui.label(label).classes("label")
                ui.label(str(summary[key])).classes("value")
    with ui.row().classes("gap-2 mt-2"):
        ui.button("Search Wallet", on_click=lambda: set_page("Wallet Search")).props("outline")
        ui.button("View Alpha Leaderboard", on_click=lambda: set_page("Alpha Leaderboard")).props("outline")
        ui.button("Open Network Explorer", on_click=lambda: set_page("Network Explorer")).props("outline")


def _render_wallet_search_page(state: dict[str, Any], settings: TraderDashboardConfig) -> None:
    from nicegui import ui

    ui.label("Wallet Search").classes("trader-section-title")
    wallet_input = ui.input("Wallet Address", value=state.get("wallet_search") or "").classes("w-[500px]")
    with ui.row().classes("items-end gap-3 w-full"):
        ui.button("Analyze", icon="search", on_click=lambda: analyze_wallet()).props("color=primary")
    result_panel = ui.column().classes("trader-panel w-full gap-2")

    def analyze_wallet() -> None:
        wallet = str(wallet_input.value or "").strip().lower()
        state["wallet_search"] = wallet
        result_panel.clear()
        if not wallet:
            ui.notify("Wallet address is required", type="warning")
            return
        try:
            details = analyze_wallet_details(
                wallet,
                scan_if_missing=True,
                traders_db_path=settings.traders_db_path,
                discovery_db_path=settings.discovery_db_path,
                wallet_export_dir=settings.wallet_export_dir,
            )
            state["wallet_details"] = details
        except Exception as exc:
            ui.notify(str(exc), type="negative")
            return
        with result_panel:
            _render_key_value_grid(details)

    if state.get("wallet_details"):
        with result_panel:
            _render_key_value_grid(state["wallet_details"])


def _render_alpha_leaderboard_page(settings: TraderDashboardConfig) -> None:
    from nicegui import ui

    rows = load_alpha_leaderboard(
        limit=settings.leaderboard_limit,
        traders_db_path=settings.traders_db_path,
        discovery_db_path=settings.discovery_db_path,
    )
    ui.label("Top Alpha Traders").classes("trader-section-title")
    if not rows:
        ui.label("No trader registry entries yet.").classes("text-slate-300")
        return
    ui.table(
        columns=[
            {"name": "wallet", "label": "Wallet", "field": "wallet", "align": "left"},
            {"name": "alpha", "label": "Alpha", "field": "alpha", "align": "left"},
            {"name": "watch_score", "label": "Watch Score", "field": "watch_score", "align": "left"},
            {"name": "profile", "label": "Profile", "field": "profile", "align": "left"},
            {"name": "classification", "label": "Classification", "field": "classification", "align": "left"},
        ],
        rows=rows,
        pagination=25,
    ).classes("w-full")


def _render_connected_traders_page(state: dict[str, Any], settings: TraderDashboardConfig) -> None:
    from nicegui import ui

    rows = load_connected_traders(
        seed_wallet=state.get("seed_wallet") or None,
        limit=settings.leaderboard_limit,
        traders_db_path=settings.traders_db_path,
        discovery_db_path=settings.discovery_db_path,
        wallet_export_dir=settings.wallet_export_dir,
    )
    ui.label("Top Connected Traders").classes("trader-section-title")
    if not rows:
        ui.label("No connected traders found for the current seed wallet.").classes("text-slate-300")
        return
    ui.table(
        columns=[
            {"name": "wallet", "label": "Wallet", "field": "wallet", "align": "left"},
            {"name": "degree", "label": "Degree", "field": "degree", "align": "left"},
            {"name": "weighted_degree", "label": "Weighted Degree", "field": "weighted_degree", "align": "left"},
            {"name": "shared_markets", "label": "Shared Markets", "field": "shared_markets", "align": "left"},
            {"name": "profile", "label": "Profile", "field": "profile", "align": "left"},
        ],
        rows=rows,
        pagination=25,
    ).classes("w-full")


def _render_insight_reports_page(state: dict[str, Any], settings: TraderDashboardConfig) -> None:
    from nicegui import ui

    ui.label("Trader Insight Report").classes("trader-section-title")
    with ui.row().classes("items-end gap-3 w-full"):
        wallet_input = ui.input("Wallet", value=state.get("insight_wallet") or state.get("seed_wallet") or "").classes("w-[500px]")
        report_panel = ui.column().classes("trader-panel w-full gap-2")

        def generate() -> None:
            wallet = str(wallet_input.value or "").strip().lower()
            state["insight_wallet"] = wallet
            report_panel.clear()
            if not wallet:
                ui.notify("Wallet is required", type="warning")
                return
            try:
                report = generate_insight_report(
                    wallet,
                    limit=settings.leaderboard_limit,
                    traders_db_path=settings.traders_db_path,
                    discovery_db_path=settings.discovery_db_path,
                    wallet_export_dir=settings.wallet_export_dir,
                )
                state["insight_report"] = report
            except Exception as exc:
                ui.notify(str(exc), type="negative")
                return
            with report_panel:
                _render_insight_report(report)

        ui.button("Generate", icon="auto_awesome", on_click=generate).props("color=primary")

    if state.get("insight_report"):
        with report_panel:
            _render_insight_report(state["insight_report"])


def _render_network_explorer_page(state: dict[str, Any], settings: TraderDashboardConfig) -> None:
    from nicegui import ui

    rows = load_network_explorer_rows(
        seed_wallet=state.get("seed_wallet") or None,
        limit=settings.network_limit,
        traders_db_path=settings.traders_db_path,
        discovery_db_path=settings.discovery_db_path,
        wallet_export_dir=settings.wallet_export_dir,
    )
    ui.label("Network Explorer").classes("trader-section-title")
    if not rows:
        ui.label("No network nodes available.").classes("text-slate-300")
        return
    ui.table(
        columns=[
            {"name": "wallet", "label": "Wallet", "field": "wallet", "align": "left"},
            {"name": "degree", "label": "Degree", "field": "degree", "align": "left"},
            {"name": "centrality", "label": "Centrality", "field": "centrality", "align": "left"},
            {"name": "cluster", "label": "Cluster", "field": "cluster", "align": "left"},
            {"name": "alpha", "label": "Alpha", "field": "alpha", "align": "left"},
        ],
        rows=rows,
        pagination=25,
    ).classes("w-full")


def _render_insight_report(report: dict[str, Any]) -> None:
    from nicegui import ui

    summary = report.get("summary") or {}
    ui.label(f"Seed: {report.get('seed_wallet', '')}").classes("text-slate-200")
    ui.label(
        f"Wallets analyzed: {summary.get('wallets_analyzed', 0)} | "
        f"Edges: {summary.get('edges', 0)} | "
        f"Clusters: {summary.get('clusters', 0)}"
    ).classes("text-slate-300")
    recommendations = report.get("recommended_traders") or []
    if not recommendations:
        ui.label("No recommendations available.").classes("text-slate-300")
        return
    ui.table(
        columns=[
            {"name": "wallet", "label": "Wallet", "field": "wallet", "align": "left"},
            {"name": "profile", "label": "Profile", "field": "profile", "align": "left"},
            {"name": "reason", "label": "Reason", "field": "reason", "align": "left"},
            {"name": "shared_markets", "label": "Shared Markets", "field": "shared_markets", "align": "left"},
            {"name": "alpha_score", "label": "Alpha Score", "field": "alpha_score", "align": "left"},
            {"name": "watch_score", "label": "Watch Score", "field": "watch_score", "align": "left"},
        ],
        rows=recommendations,
        pagination=25,
    ).classes("w-full")


def _render_key_value_grid(details: dict[str, Any]) -> None:
    from nicegui import ui

    for key, label in (
        ("wallet", "Wallet"),
        ("classification", "Classification"),
        ("confidence", "Confidence"),
        ("watch_score", "Watch Score"),
        ("alpha_score", "Alpha Score"),
        ("profile", "Profile"),
        ("markets_traded", "Markets Traded"),
        ("shared_markets", "Shared Markets"),
        ("btc_volume", "BTC Volume"),
        ("eth_volume", "ETH Volume"),
        ("sol_volume", "SOL Volume"),
        ("merge_count", "Merge Count"),
        ("redeem_count", "Redeem Count"),
    ):
        ui.label(f"{label}: {details.get(key, '')}").classes("text-slate-200")


def _handle_discovery(state: dict[str, Any], settings: TraderDashboardConfig) -> None:
    from nicegui import ui

    try:
        result = run_discovery_action(
            state.get("seed_wallet") or None,
            limit=settings.leaderboard_limit,
            traders_db_path=settings.traders_db_path,
            discovery_db_path=settings.discovery_db_path,
        )
        state["last_action"] = (
            f"Discovery complete: {result.get('wallets_discovered', 0)} wallets, "
            f"{result.get('new_wallets', 0)} new"
        )
        ui.notify(state["last_action"], type="positive")
    except Exception as exc:
        ui.notify(str(exc), type="negative")


def _handle_profiling(state: dict[str, Any], settings: TraderDashboardConfig) -> None:
    from nicegui import ui

    try:
        result = run_profiling_action(
            seed_wallet=state.get("seed_wallet") or None,
            limit=settings.leaderboard_limit,
            traders_db_path=settings.traders_db_path,
            discovery_db_path=settings.discovery_db_path,
            wallet_export_dir=settings.wallet_export_dir,
        )
        state["last_action"] = f"Profiling complete: {result.get('profiles_generated', 0)} profiles generated"
        ui.notify(state["last_action"], type="positive")
    except Exception as exc:
        ui.notify(str(exc), type="negative")


def _handle_refresh_insights(state: dict[str, Any], settings: TraderDashboardConfig) -> None:
    from nicegui import ui

    wallet = str(state.get("insight_wallet") or state.get("seed_wallet") or "").strip().lower()
    if not wallet:
        ui.notify("Set a seed or insight wallet first", type="warning")
        return
    try:
        result = refresh_insights_action(
            wallet,
            limit=settings.leaderboard_limit,
            traders_db_path=settings.traders_db_path,
            discovery_db_path=settings.discovery_db_path,
            wallet_export_dir=settings.wallet_export_dir,
        )
        state["insight_report"] = result
        state["last_action"] = f"Insights refreshed for {wallet}"
        ui.notify(state["last_action"], type="positive")
    except Exception as exc:
        ui.notify(str(exc), type="negative")


def _profile_label_from_alpha_row(row: dict[str, Any]) -> str:
    return derive_specialization(
        classification=str(row.get("classification") or "unknown"),
        confidence=float(row.get("confidence") or 0.0),
        activity_count=int(row.get("activity_count") or 0),
        markets_traded=int(row.get("markets_traded") or 0),
        shared_markets=int(row.get("shared_markets") or 0),
        overlap_ratio=float(row.get("overlap_ratio") or 0.0),
        merge_count=int(row.get("merge_count") or 0),
        redeem_count=int(row.get("redeem_count") or 0),
        btc_volume=float((row.get("asset_breakdown") or {}).get("BTC", 0.0)),
        eth_volume=float((row.get("asset_breakdown") or {}).get("ETH", 0.0)),
        sol_volume=float((row.get("asset_breakdown") or {}).get("SOL", 0.0)),
        other_volume=float((row.get("asset_breakdown") or {}).get("OTHER", 0.0)),
    )


def _profile_label_for_wallet(
    wallet: str,
    *,
    traders_db_path: str | Path,
    discovery_db_path: str | Path,
) -> dict[str, Any]:
    alpha = build_trader_alpha_report(
        wallet,
        traders_db_path=traders_db_path,
        discovery_db_path=discovery_db_path,
    )
    return {
        "profile": derive_specialization(
            classification=alpha.classification,
            confidence=alpha.confidence,
            activity_count=alpha.activity_count,
            markets_traded=alpha.markets_traded,
            shared_markets=alpha.shared_markets,
            overlap_ratio=alpha.overlap_ratio,
            merge_count=alpha.merge_count,
            redeem_count=alpha.redeem_count,
            btc_volume=alpha.asset_breakdown.get("BTC", 0.0),
            eth_volume=alpha.asset_breakdown.get("ETH", 0.0),
            sol_volume=alpha.asset_breakdown.get("SOL", 0.0),
            other_volume=alpha.asset_breakdown.get("OTHER", 0.0),
        ),
        "shared_markets": alpha.shared_markets,
    }


def _discovery_wallet_count(discovery_db_path: str | Path) -> int:
    path = Path(discovery_db_path)
    if not path.exists():
        return 0
    with closing_connection(path) as conn:
        row = conn.execute("SELECT COUNT(*) AS count FROM discovered_wallets").fetchone()
    return int(row["count"]) if row else 0


def _normalize_wallet(wallet: str | None) -> str:
    return str(wallet or "").strip().lower()
