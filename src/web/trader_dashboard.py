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
from src.web.trader_dashboard_styles import OVERVIEW_TABLE_LIMIT, TRADER_NAV_ITEMS, TRADER_TERMINAL_CSS

__all__ = [
    "DEFAULT_TRADER_DASHBOARD_HOST",
    "DEFAULT_TRADER_DASHBOARD_PORT",
    "TRADER_NAV_ITEMS",
    "TraderDashboardConfig",
    "analyze_wallet_details",
    "create_trader_dashboard",
    "generate_insight_report",
    "load_alpha_leaderboard",
    "load_bridge_traders",
    "load_connected_traders",
    "load_network_explorer_rows",
    "load_overview_kpis",
    "load_overview_recommendations",
    "load_profile_categories",
    "load_registry_summary",
    "refresh_insights_action",
    "run_discovery_action",
    "run_profiling_action",
    "run_trader_dashboard",
    "trader_home_summary",
]

DEFAULT_TRADER_DASHBOARD_HOST = "127.0.0.1"
DEFAULT_TRADER_DASHBOARD_PORT = 8788
DEFAULT_LEADERBOARD_LIMIT = 25
DEFAULT_NETWORK_LIMIT = 50


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


def load_registry_summary(*, traders_db_path: str | Path = DEFAULT_TRADERS_DB) -> dict[str, Any]:
    registry = registry_stats(db_path=str(traders_db_path))
    by_class = {row["classification"]: int(row["count"]) for row in registry.get("by_classification", [])}
    return {
        "traders_profiled": int(registry.get("total_traders") or 0),
        "alpha_reports": int(registry.get("total_traders") or 0),
        "market_makers": by_class.get("market_maker", 0),
        "arbitrage_traders": by_class.get("arbitrage_trader", 0),
    }


def load_overview_kpis(
    *,
    seed_wallet: str | None = None,
    traders_db_path: str | Path = DEFAULT_TRADERS_DB,
    discovery_db_path: str | Path = DEFAULT_TRADER_DISCOVERY_DB,
    wallet_export_dir: str | Path = DEFAULT_WALLET_EXPORT_DIR,
) -> dict[str, Any]:
    alpha_rows = load_alpha_leaderboard(
        limit=OVERVIEW_TABLE_LIMIT,
        traders_db_path=traders_db_path,
        discovery_db_path=discovery_db_path,
    )
    registry = load_registry_summary(traders_db_path=traders_db_path)
    discovered = _discovery_wallet_count(discovery_db_path)
    top_alpha = alpha_rows[0]["alpha"] if alpha_rows else 0
    top_watch = max((row["watch_score"] for row in alpha_rows), default=0)
    return {
        "top_alpha": top_alpha,
        "top_alpha_footnote": "leading registry" if alpha_rows else "no registry data",
        "top_watch_score": top_watch,
        "top_watch_footnote": "highest watch score",
        "traders_profiled": registry["traders_profiled"],
        "traders_profiled_footnote": f"{registry['market_makers']} market makers",
        "discovery_candidates": discovered,
        "discovery_footnote": _discovery_footnote(seed_wallet, discovery_db_path, wallet_export_dir, traders_db_path),
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
                "wallet": _short_wallet(item["wallet"]),
                "wallet_full": item["wallet"],
                "alpha": item["alpha_score"],
                "watch": item["watch_score"],
                "watch_score": item["watch_score"],
                "profile": profile,
                "classification": item["classification"],
            }
        )
    rows.sort(key=lambda row: (-row["alpha"], -row["watch"], row["wallet_full"]))
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
        node = network.nodes.get(item["wallet"])
        profile = _profile_label_for_wallet(
            item["wallet"],
            traders_db_path=traders_db_path,
            discovery_db_path=discovery_db_path,
        )
        rows.append(
            {
                "wallet": _short_wallet(item["wallet"]),
                "wallet_full": item["wallet"],
                "degree": item.get("degree", 0),
                "weighted_degree": item.get("weighted_degree", 0.0),
                "shared_markets": profile["shared_markets"],
                "alpha": node.alpha_score if node else 0,
                "profile": profile["profile"],
            }
        )
    return rows


def load_bridge_traders(
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
    for item in summary["top_bridge_wallets"]:
        node = network.nodes.get(item["wallet"])
        rows.append(
            {
                "wallet": _short_wallet(item["wallet"]),
                "wallet_full": item["wallet"],
                "bridge_score": round(float(item.get("bridge_score") or 0.0), 4),
                "cluster": node.cluster_id if node else -1,
                "degree": item.get("degree", 0),
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
            "wallet": _short_wallet(node.wallet),
            "wallet_full": node.wallet,
            "degree": node.degree,
            "centrality": round(node.centrality_score, 4),
            "cluster": node.cluster_id,
            "alpha": node.alpha_score,
            "watch_score": node.watch_score,
        }
        for node in ordered[: max(int(limit or DEFAULT_NETWORK_LIMIT), 0)]
    ]


def load_profile_categories(
    *,
    seed_wallet: str | None = None,
    limit: int = DEFAULT_LEADERBOARD_LIMIT,
    traders_db_path: str | Path = DEFAULT_TRADERS_DB,
    discovery_db_path: str | Path = DEFAULT_TRADER_DISCOVERY_DB,
    wallet_export_dir: str | Path = DEFAULT_WALLET_EXPORT_DIR,
) -> dict[str, list[dict[str, Any]]]:
    seed_wallet = _normalize_wallet(seed_wallet) or None
    if seed_wallet:
        report = build_trader_insight_report(
            seed_wallet,
            limit=limit,
            scan_if_missing=False,
            traders_db_path=traders_db_path,
            discovery_db_path=discovery_db_path,
            wallet_export_dir=wallet_export_dir,
        )
        arbitrage = _rows_from_profiles(report, "cross-market arbitrage", limit)
        if not arbitrage:
            arbitrage = [
                _compact_profile_row(row)
                for row in report.get("top_connected_traders", [])
                if row.get("classification") == "arbitrage_trader"
            ][:limit]
        return {
            "btc_specialists": [_compact_profile_row(row) for row in report.get("top_btc_specialists", [])[:limit]],
            "directional_traders": [_compact_profile_row(row) for row in report.get("top_directional_traders", [])[:limit]],
            "market_makers": [_compact_profile_row(row) for row in report.get("top_market_makers", [])[:limit]],
            "arbitrage_traders": arbitrage,
        }

    profiles = profile_traders(
        limit=limit,
        traders_db_path=traders_db_path,
        discovery_db_path=discovery_db_path,
        wallet_export_dir=wallet_export_dir,
    )
    return {
        "btc_specialists": [_profile_row(item) for item in profiles if item.get("profile") == "btc specialist"][:limit],
        "directional_traders": [_profile_row(item) for item in profiles if item.get("profile") == "directional trader"][:limit],
        "market_makers": [
            _profile_row(item)
            for item in profiles
            if item.get("profile") in {"crypto market maker", "market maker", "high-frequency inventory manager"}
            or item.get("classification") == "market_maker"
        ][:limit],
        "arbitrage_traders": [
            _profile_row(item)
            for item in profiles
            if item.get("profile") in {"cross-market arbitrage", "arbitrage trader"}
            or item.get("classification") == "arbitrage_trader"
        ][:limit],
    }


def load_overview_recommendations(
    *,
    seed_wallet: str | None = None,
    limit: int = OVERVIEW_TABLE_LIMIT,
    traders_db_path: str | Path = DEFAULT_TRADERS_DB,
    discovery_db_path: str | Path = DEFAULT_TRADER_DISCOVERY_DB,
    wallet_export_dir: str | Path = DEFAULT_WALLET_EXPORT_DIR,
) -> list[dict[str, Any]]:
    seed_wallet = _normalize_wallet(seed_wallet) or None
    if not seed_wallet:
        return []
    report = build_trader_insight_report(
        seed_wallet,
        limit=limit,
        scan_if_missing=False,
        traders_db_path=traders_db_path,
        discovery_db_path=discovery_db_path,
        wallet_export_dir=wallet_export_dir,
    )
    return [
        {
            "wallet": _short_wallet(item["wallet"]),
            "wallet_full": item["wallet"],
            "reason": item.get("reason", ""),
            "alpha": item.get("alpha_score", 0),
            "shared_markets": item.get("shared_markets", 0),
        }
        for item in report.get("recommended_traders", [])[:limit]
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
        "strongest_shared_market_relationships": report.get("strongest_shared_market_relationships", []),
        "top_alpha_traders": report.get("top_alpha_traders", []),
        "top_study_targets": report.get("recommended_traders", []),
    }


def create_trader_dashboard(config: TraderDashboardConfig | None = None) -> None:
    from nicegui import ui

    settings = config or TraderDashboardConfig()
    state: dict[str, Any] = {
        "page": "Overview",
        "seed_wallet": settings.seed_wallet,
        "wallet_search": settings.seed_wallet,
        "insight_wallet": settings.seed_wallet,
        "last_action": "",
        "wallet_details": None,
        "insight_report": None,
        "last_discovery_new": 0,
    }

    @ui.page("/")
    def trader_dashboard_page() -> None:
        ui.add_head_html(f"<style>{TRADER_TERMINAL_CSS}</style>")
        shell = ui.column().classes("tt-app w-full")
        center_panel = ui.column().classes("tt-center tt-center-stack w-full")
        sidebar_metrics_box = ui.column().classes("w-full")
        wallet_details_box = ui.column().classes("w-full")

        def render_sidebar_metrics() -> None:
            sidebar_metrics_box.clear()
            home = trader_home_summary(
                seed_wallet=state.get("seed_wallet") or None,
                traders_db_path=settings.traders_db_path,
                discovery_db_path=settings.discovery_db_path,
                wallet_export_dir=settings.wallet_export_dir,
            )
            registry = load_registry_summary(traders_db_path=settings.traders_db_path)
            with sidebar_metrics_box:
                with ui.element("div").classes("tt-card w-full"):
                    ui.label("Network Summary").classes("tt-section-label")
                    with ui.element("div").classes("tt-metric-grid"):
                        for key, label in (
                            ("wallets_discovered", "Discovered"),
                            ("network_nodes", "Nodes"),
                            ("network_edges", "Edges"),
                            ("clusters", "Clusters"),
                        ):
                            with ui.element("div").classes("tt-metric"):
                                ui.label(label).classes("label")
                                ui.label(str(home[key])).classes("value")
                with ui.element("div").classes("tt-card w-full"):
                    ui.label("Registry Summary").classes("tt-section-label")
                    with ui.element("div").classes("tt-metric-grid"):
                        for key, label in (
                            ("traders_profiled", "Profiled"),
                            ("alpha_reports", "Alpha Reports"),
                            ("market_makers", "Market Makers"),
                            ("arbitrage_traders", "Arbitrage"),
                        ):
                            with ui.element("div").classes("tt-metric"):
                                ui.label(label).classes("label")
                                ui.label(str(registry[key])).classes("value")

        def render_wallet_details_panel() -> None:
            wallet_details_box.clear()
            with wallet_details_box:
                if state.get("wallet_details"):
                    _render_wallet_details(state["wallet_details"])
                else:
                    ui.label("Select a wallet to inspect classification, alpha, and asset mix.").classes("tt-status")

        def render_center() -> None:
            center_panel.clear()
            with center_panel:
                page = state["page"]
                if page == "Overview":
                    _render_overview_page(state, settings)
                elif page == "Network":
                    _render_network_page(state, settings)
                elif page == "Profiles":
                    _render_profiles_page(state, settings)
                elif page == "Insights":
                    _render_insights_page(state, settings, render_center)

        def set_page(page: str) -> None:
            state["page"] = page
            render_center()

        def refresh_all() -> None:
            render_center()
            render_sidebar_metrics()
            render_wallet_details_panel()

        with shell:
            _render_topbar(state, set_page)
            with ui.element("div").classes("tt-layout w-full"):
                with ui.column().classes("tt-sidebar tt-stack"):
                    _render_sidebar_actions(state, settings, refresh_all)
                    sidebar_metrics_box
                with ui.column().classes("tt-center w-full"):
                    center_panel
                with ui.column().classes("tt-right"):
                    with ui.element("div").classes("tt-wallet-panel tt-card w-full"):
                        ui.label("Wallet Intelligence").classes("tt-section-title")
                        wallet_input = ui.input(
                            placeholder="Search wallet",
                            value=state.get("wallet_search") or "",
                        ).classes("tt-input w-full").props("dense outlined dark")

                        def analyze_wallet() -> None:
                            wallet = str(wallet_input.value or "").strip().lower()
                            state["wallet_search"] = wallet
                            if not wallet:
                                ui.notify("Wallet address is required", type="warning")
                                return
                            try:
                                state["wallet_details"] = analyze_wallet_details(
                                    wallet,
                                    scan_if_missing=True,
                                    traders_db_path=settings.traders_db_path,
                                    discovery_db_path=settings.discovery_db_path,
                                    wallet_export_dir=settings.wallet_export_dir,
                                )
                            except Exception as exc:
                                ui.notify(str(exc), type="negative")
                                return
                            render_wallet_details_panel()

                        ui.button("Analyze", on_click=analyze_wallet).classes("tt-btn-primary tt-btn-block")
                        wallet_details_box

        render_sidebar_metrics()
        render_center()
        render_wallet_details_panel()

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


def _render_topbar(state: dict[str, Any], set_page) -> None:
    from nicegui import ui

    with ui.element("div").classes("tt-topbar w-full"):
        with ui.column().classes("gap-0"):
            ui.label("Trader Intelligence Center").classes("tt-brand-title")
            ui.label("Research terminal for discovery, profiling, network, and alpha").classes("tt-brand-sub")
        with ui.element("div").classes("tt-nav"):
            for item in TRADER_NAV_ITEMS:
                active = state["page"] == item
                ui.button(
                    item,
                    on_click=lambda item=item: set_page(item),
                ).classes("tt-nav-btn active" if active else "tt-nav-btn")
        ui.label("Research Only").classes("tt-badge")


def _render_sidebar_actions(state: dict[str, Any], settings: TraderDashboardConfig, refresh_all) -> None:
    from nicegui import ui

    with ui.element("div").classes("tt-card w-full"):
        ui.label("Seed Wallet").classes("tt-section-label")
        ui.input(
            placeholder="0x...",
            value=state.get("seed_wallet") or "",
        ).bind_value(state, "seed_wallet").classes("tt-input w-full").props("dense outlined dark")

        with ui.column().classes("tt-stack w-full"):
            ui.button("Run Discovery", on_click=lambda: _handle_discovery(state, settings, refresh_all)).classes("tt-btn-primary tt-btn-block")
            ui.button("Run Profiling", on_click=lambda: _handle_profiling(state, settings, refresh_all)).classes("tt-btn-secondary tt-btn-block")
            ui.button("Refresh Insights", on_click=lambda: _handle_refresh_insights(state, settings, refresh_all)).classes("tt-btn-secondary tt-btn-block")
            ui.button("Refresh View", on_click=refresh_all).classes("tt-btn-secondary tt-btn-block")
        ui.label(state.get("last_action") or "").classes("tt-status")


def _render_overview_page(state: dict[str, Any], settings: TraderDashboardConfig) -> None:
    from nicegui import ui

    kpis = load_overview_kpis(
        seed_wallet=state.get("seed_wallet") or None,
        traders_db_path=settings.traders_db_path,
        discovery_db_path=settings.discovery_db_path,
        wallet_export_dir=settings.wallet_export_dir,
    )
    alpha_rows = load_alpha_leaderboard(
        limit=OVERVIEW_TABLE_LIMIT,
        traders_db_path=settings.traders_db_path,
        discovery_db_path=settings.discovery_db_path,
    )
    recommendations = load_overview_recommendations(
        seed_wallet=state.get("seed_wallet") or None,
        limit=OVERVIEW_TABLE_LIMIT,
        traders_db_path=settings.traders_db_path,
        discovery_db_path=settings.discovery_db_path,
        wallet_export_dir=settings.wallet_export_dir,
    )

    with ui.element("div").classes("tt-kpi-grid w-full"):
        _kpi_card("Top Alpha", str(kpis["top_alpha"]), kpis["top_alpha_footnote"], "info")
        _kpi_card("Top Watch Score", str(kpis["top_watch_score"]), kpis["top_watch_footnote"])
        _kpi_card("Traders Profiled", _format_count(kpis["traders_profiled"]), kpis["traders_profiled_footnote"], "positive")
        _kpi_card("Discovery Candidates", _format_count(kpis["discovery_candidates"]), kpis["discovery_footnote"])

    with ui.element("div").classes("tt-card w-full"):
        ui.label("Top Alpha Traders").classes("tt-section-title")
        _render_table(
            [
                {"name": "wallet", "label": "Wallet", "field": "wallet", "align": "left"},
                {"name": "alpha", "label": "Alpha", "field": "alpha", "align": "left"},
                {"name": "watch", "label": "Watch", "field": "watch", "align": "left"},
                {"name": "profile", "label": "Profile", "field": "profile", "align": "left"},
                {"name": "classification", "label": "Classification", "field": "classification", "align": "left"},
            ],
            alpha_rows,
            pagination=OVERVIEW_TABLE_LIMIT,
        )

    with ui.element("div").classes("tt-card w-full"):
        ui.label("Recommended Traders").classes("tt-section-title")
        if recommendations:
            _render_table(
                [
                    {"name": "wallet", "label": "Wallet", "field": "wallet", "align": "left"},
                    {"name": "reason", "label": "Reason", "field": "reason", "align": "left"},
                    {"name": "alpha", "label": "Alpha", "field": "alpha", "align": "left"},
                    {"name": "shared_markets", "label": "Shared Markets", "field": "shared_markets", "align": "left"},
                ],
                recommendations,
                pagination=OVERVIEW_TABLE_LIMIT,
            )
        else:
            ui.label("Set a seed wallet to generate recommendations.").classes("tt-status")


def _render_network_page(state: dict[str, Any], settings: TraderDashboardConfig) -> None:
    from nicegui import ui

    connected = load_connected_traders(
        seed_wallet=state.get("seed_wallet") or None,
        limit=settings.leaderboard_limit,
        traders_db_path=settings.traders_db_path,
        discovery_db_path=settings.discovery_db_path,
        wallet_export_dir=settings.wallet_export_dir,
    )
    bridges = load_bridge_traders(
        seed_wallet=state.get("seed_wallet") or None,
        limit=settings.leaderboard_limit,
        traders_db_path=settings.traders_db_path,
        discovery_db_path=settings.discovery_db_path,
        wallet_export_dir=settings.wallet_export_dir,
    )
    explorer = load_network_explorer_rows(
        seed_wallet=state.get("seed_wallet") or None,
        limit=settings.network_limit,
        traders_db_path=settings.traders_db_path,
        discovery_db_path=settings.discovery_db_path,
        wallet_export_dir=settings.wallet_export_dir,
    )

    with ui.element("div").classes("tt-card w-full"):
        ui.label("Top Connected Traders").classes("tt-section-title")
        _render_table(
            [
                {"name": "wallet", "label": "Wallet", "field": "wallet", "align": "left"},
                {"name": "degree", "label": "Degree", "field": "degree", "align": "left"},
                {"name": "weighted_degree", "label": "Weighted Degree", "field": "weighted_degree", "align": "left"},
                {"name": "shared_markets", "label": "Shared Markets", "field": "shared_markets", "align": "left"},
                {"name": "alpha", "label": "Alpha", "field": "alpha", "align": "left"},
            ],
            connected,
        )

    with ui.element("div").classes("tt-card w-full"):
        ui.label("Top Bridge Traders").classes("tt-section-title")
        _render_table(
            [
                {"name": "wallet", "label": "Wallet", "field": "wallet", "align": "left"},
                {"name": "bridge_score", "label": "Bridge Score", "field": "bridge_score", "align": "left"},
                {"name": "cluster", "label": "Cluster", "field": "cluster", "align": "left"},
                {"name": "degree", "label": "Degree", "field": "degree", "align": "left"},
            ],
            bridges,
        )

    with ui.element("div").classes("tt-card w-full"):
        ui.label("Network Explorer").classes("tt-section-title")
        _render_table(
            [
                {"name": "wallet", "label": "Wallet", "field": "wallet", "align": "left"},
                {"name": "degree", "label": "Degree", "field": "degree", "align": "left"},
                {"name": "centrality", "label": "Centrality", "field": "centrality", "align": "left"},
                {"name": "cluster", "label": "Cluster", "field": "cluster", "align": "left"},
                {"name": "alpha", "label": "Alpha", "field": "alpha", "align": "left"},
                {"name": "watch_score", "label": "Watch Score", "field": "watch_score", "align": "left"},
            ],
            explorer,
            pagination=settings.network_limit,
        )


def _render_profiles_page(state: dict[str, Any], settings: TraderDashboardConfig) -> None:
    from nicegui import ui

    categories = load_profile_categories(
        seed_wallet=state.get("seed_wallet") or None,
        limit=settings.leaderboard_limit,
        traders_db_path=settings.traders_db_path,
        discovery_db_path=settings.discovery_db_path,
        wallet_export_dir=settings.wallet_export_dir,
    )
    sections = (
        ("btc_specialists", "Top BTC Specialists"),
        ("directional_traders", "Top Directional Traders"),
        ("market_makers", "Top Market Makers"),
        ("arbitrage_traders", "Top Arbitrage Traders"),
    )
    for key, title in sections:
        rows = categories.get(key, [])
        with ui.element("div").classes("tt-card w-full"):
            ui.label(title).classes("tt-section-title")
            if rows:
                _render_table(
                    [
                        {"name": "wallet", "label": "Wallet", "field": "wallet", "align": "left"},
                        {"name": "profile", "label": "Profile", "field": "profile", "align": "left"},
                        {"name": "alpha", "label": "Alpha", "field": "alpha", "align": "left"},
                        {"name": "watch", "label": "Watch", "field": "watch", "align": "left"},
                        {"name": "shared_markets", "label": "Shared Markets", "field": "shared_markets", "align": "left"},
                    ],
                    rows,
                )
            else:
                ui.label("No profiles in this category yet.").classes("tt-status")


def _render_insights_page(state: dict[str, Any], settings: TraderDashboardConfig, render_center) -> None:
    from nicegui import ui

    with ui.element("div").classes("tt-card w-full"):
        ui.label("Trader Insight Report").classes("tt-section-title")
        with ui.row().classes("items-end gap-2 w-full"):
            wallet_input = ui.input(
                placeholder="Seed wallet",
                value=state.get("insight_wallet") or state.get("seed_wallet") or "",
            ).classes("tt-input w-full").props("dense outlined dark")

            def generate() -> None:
                wallet = str(wallet_input.value or "").strip().lower()
                state["insight_wallet"] = wallet
                if not wallet:
                    ui.notify("Wallet is required", type="warning")
                    return
                try:
                    state["insight_report"] = generate_insight_report(
                        wallet,
                        limit=settings.leaderboard_limit,
                        traders_db_path=settings.traders_db_path,
                        discovery_db_path=settings.discovery_db_path,
                        wallet_export_dir=settings.wallet_export_dir,
                    )
                    ui.notify(f"Insight report generated for {wallet}", type="positive")
                    render_center()
                except Exception as exc:
                    ui.notify(str(exc), type="negative")

            ui.button("Generate", on_click=generate).classes("tt-btn-primary")

    report = state.get("insight_report")
    if not report:
        ui.label("Generate an insight report from the Insights page.").classes("tt-status")
        return

    insight_sections = (
        ("recommended_traders", "Recommended Traders", [
            {"name": "wallet", "label": "Wallet", "field": "wallet", "align": "left"},
            {"name": "profile", "label": "Profile", "field": "profile", "align": "left"},
            {"name": "reason", "label": "Reason", "field": "reason", "align": "left"},
            {"name": "alpha_score", "label": "Alpha", "field": "alpha_score", "align": "left"},
            {"name": "shared_markets", "label": "Shared Markets", "field": "shared_markets", "align": "left"},
        ]),
        ("strongest_shared_market_relationships", "Strongest Shared Market Relationships", [
            {"name": "wallet_a", "label": "Wallet A", "field": "wallet_a", "align": "left"},
            {"name": "wallet_b", "label": "Wallet B", "field": "wallet_b", "align": "left"},
            {"name": "shared_markets", "label": "Shared Markets", "field": "shared_markets", "align": "left"},
            {"name": "weight", "label": "Weight", "field": "weight", "align": "left"},
        ]),
        ("top_alpha_traders", "Top Alpha Traders", [
            {"name": "wallet", "label": "Wallet", "field": "wallet", "align": "left"},
            {"name": "alpha_score", "label": "Alpha", "field": "alpha_score", "align": "left"},
            {"name": "watch_score", "label": "Watch", "field": "watch_score", "align": "left"},
            {"name": "profile", "label": "Profile", "field": "profile", "align": "left"},
        ]),
    )
    for key, title, columns in insight_sections:
        rows = report.get(key, [])
        if key == "strongest_shared_market_relationships":
            rows = [_relationship_row(row) for row in rows]
        elif key in {"recommended_traders", "top_alpha_traders"}:
            rows = [_insight_table_row(row) for row in rows]
        with ui.element("div").classes("tt-card w-full"):
            ui.label(title).classes("tt-section-title")
            if rows:
                _render_table(columns, rows)
            else:
                ui.label("No rows available.").classes("tt-status")

    study_targets = [_insight_table_row(row) for row in report.get("recommended_traders", [])[: settings.leaderboard_limit]]
    with ui.element("div").classes("tt-card w-full"):
        ui.label("Top Study Targets").classes("tt-section-title")
        if study_targets:
            _render_table(
                [
                    {"name": "wallet", "label": "Wallet", "field": "wallet", "align": "left"},
                    {"name": "reason", "label": "Reason", "field": "reason", "align": "left"},
                    {"name": "alpha_score", "label": "Alpha", "field": "alpha_score", "align": "left"},
                    {"name": "watch_score", "label": "Watch", "field": "watch_score", "align": "left"},
                ],
                study_targets,
            )
        else:
            ui.label("No study targets available.").classes("tt-status")


def _render_wallet_details(details: dict[str, Any]) -> None:
    from nicegui import ui

    rows = (
        ("Classification", details.get("classification"), ""),
        ("Confidence", f"{float(details.get('confidence', 0)):.2f}", ""),
        ("Watch Score", details.get("watch_score"), "info"),
        ("Alpha Score", details.get("alpha_score"), "positive"),
        ("Profile", details.get("profile"), ""),
        ("Markets Traded", details.get("markets_traded"), ""),
        ("Shared Markets", details.get("shared_markets"), ""),
        ("Merge Count", details.get("merge_count"), ""),
        ("Redeem Count", details.get("redeem_count"), ""),
    )
    for key, value, tone in rows:
        with ui.element("div").classes("metric-row"):
            ui.label(str(key)).classes("metric-key")
            ui.label(str(value)).classes(f"metric-val {tone}".strip())

    ui.label("Asset Breakdown").classes("tt-section-label")
    with ui.element("div").classes("tt-asset-grid w-full"):
        for asset, value in (
            ("BTC", details.get("btc_volume", 0)),
            ("ETH", details.get("eth_volume", 0)),
            ("SOL", details.get("sol_volume", 0)),
        ):
            with ui.element("div").classes("tt-asset-card"):
                ui.label(asset).classes("label")
                ui.label(_format_volume(value)).classes("value")


def _render_table(columns: list[dict[str, Any]], rows: list[dict[str, Any]], pagination: int = 15) -> None:
    from nicegui import ui

    if not rows:
        ui.label("No data available.").classes("tt-status")
        return
    row_key = _table_row_key(rows[0])
    ui.table(columns=columns, rows=rows, pagination=pagination, row_key=row_key).classes("tt-table w-full").props("dense flat dark")


def _kpi_card(label: str, value: str, footnote: str, foot_class: str = "") -> None:
    from nicegui import ui

    with ui.element("div").classes("tt-kpi"):
        ui.label(label).classes("label")
        ui.label(value).classes("value")
        ui.label(footnote).classes(f"foot {foot_class}".strip())


def _handle_discovery(state: dict[str, Any], settings: TraderDashboardConfig, refresh_all) -> None:
    from nicegui import ui

    try:
        result = run_discovery_action(
            state.get("seed_wallet") or None,
            limit=settings.leaderboard_limit,
            traders_db_path=settings.traders_db_path,
            discovery_db_path=settings.discovery_db_path,
        )
        state["last_discovery_new"] = int(result.get("new_wallets") or 0)
        state["last_action"] = (
            f"Discovery: {result.get('wallets_discovered', 0)} wallets, "
            f"{result.get('new_wallets', 0)} new"
        )
        ui.notify(state["last_action"], type="positive")
        refresh_all()
    except Exception as exc:
        ui.notify(str(exc), type="negative")


def _handle_profiling(state: dict[str, Any], settings: TraderDashboardConfig, refresh_all) -> None:
    from nicegui import ui

    try:
        result = run_profiling_action(
            seed_wallet=state.get("seed_wallet") or None,
            limit=settings.leaderboard_limit,
            traders_db_path=settings.traders_db_path,
            discovery_db_path=settings.discovery_db_path,
            wallet_export_dir=settings.wallet_export_dir,
        )
        state["last_action"] = f"Profiling: {result.get('profiles_generated', 0)} profiles generated"
        ui.notify(state["last_action"], type="positive")
        refresh_all()
    except Exception as exc:
        ui.notify(str(exc), type="negative")


def _handle_refresh_insights(state: dict[str, Any], settings: TraderDashboardConfig, refresh_all) -> None:
    from nicegui import ui

    wallet = str(state.get("insight_wallet") or state.get("seed_wallet") or "").strip().lower()
    if not wallet:
        ui.notify("Set a seed or insight wallet first", type="warning")
        return
    try:
        state["insight_report"] = refresh_insights_action(
            wallet,
            limit=settings.leaderboard_limit,
            traders_db_path=settings.traders_db_path,
            discovery_db_path=settings.discovery_db_path,
            wallet_export_dir=settings.wallet_export_dir,
        )
        state["last_action"] = f"Insights refreshed for {wallet}"
        ui.notify(state["last_action"], type="positive")
        refresh_all()
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


def _compact_profile_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "wallet": _short_wallet(row.get("wallet", "")),
        "wallet_full": row.get("wallet", ""),
        "profile": row.get("profile", ""),
        "alpha": row.get("alpha_score", 0),
        "watch": row.get("watch_score", 0),
        "shared_markets": row.get("shared_markets", 0),
    }


def _profile_row(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "wallet": _short_wallet(item.get("wallet", "")),
        "wallet_full": item.get("wallet", ""),
        "profile": item.get("profile", ""),
        "alpha": item.get("alpha_score", 0),
        "watch": item.get("watch_score", 0),
        "shared_markets": item.get("shared_markets", 0),
    }


def _rows_from_profiles(report: dict[str, Any], profile_name: str, limit: int) -> list[dict[str, Any]]:
    rows = [
        _compact_profile_row(row)
        for row in report.get("top_connected_traders", [])
        if row.get("profile") == profile_name
    ]
    return rows[:limit]


def _insight_table_row(row: dict[str, Any]) -> dict[str, Any]:
    wallet = row.get("wallet", "")
    return {
        **row,
        "wallet": _short_wallet(wallet),
        "wallet_full": wallet,
    }


def _relationship_row(row: dict[str, Any]) -> dict[str, Any]:
    wallet_a = row.get("wallet_a", "")
    wallet_b = row.get("wallet_b", "")
    return {
        "wallet_a": _short_wallet(wallet_a),
        "wallet_b": _short_wallet(wallet_b),
        "wallet_a_full": wallet_a,
        "wallet_b_full": wallet_b,
        "shared_markets": row.get("shared_markets", row.get("weight", 0)),
        "weight": row.get("weight", 0),
    }


def _table_row_key(row: dict[str, Any]) -> str:
    for key in ("wallet_full", "wallet", "wallet_a_full", "wallet_b_full"):
        if row.get(key):
            return key
    return next(iter(row))


def _discovery_wallet_count(discovery_db_path: str | Path) -> int:
    path = Path(discovery_db_path)
    if not path.exists():
        return 0
    with closing_connection(path) as conn:
        row = conn.execute("SELECT COUNT(*) AS count FROM discovered_wallets").fetchone()
    return int(row["count"]) if row else 0


def _discovery_footnote(
    seed_wallet: str | None,
    discovery_db_path: str | Path,
    wallet_export_dir: str | Path,
    traders_db_path: str | Path,
) -> str:
    if seed_wallet:
        network = build_trader_network(
            wallet=seed_wallet,
            traders_db_path=traders_db_path,
            discovery_db_path=discovery_db_path,
            wallet_export_dir=wallet_export_dir,
        )
        return f"{len(network.nodes)} nodes in seed network"
    return "global discovery pool"


def _short_wallet(wallet: str) -> str:
    wallet = str(wallet or "")
    if len(wallet) <= 14:
        return wallet
    return f"{wallet[:8]}...{wallet[-6:]}"


def _format_count(value: int) -> str:
    return f"{int(value):,}"


def _format_volume(value: Any) -> str:
    try:
        number = float(value or 0)
    except (TypeError, ValueError):
        return "0"
    if number >= 1000:
        return f"{number:,.0f}"
    return f"{number:.2f}"


def _normalize_wallet(wallet: str | None) -> str:
    return str(wallet or "").strip().lower()
