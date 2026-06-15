from __future__ import annotations

import sys
from pathlib import Path

import pytest

from src.analysis.trader_discovery import TraderDiscoveryCandidate, save_discovery_candidate, save_discovery_relationships
from src.analysis.trader_registry import save_wallet_report
from src.web.trader_dashboard import (
    DEFAULT_TRADER_DASHBOARD_HOST,
    DEFAULT_TRADER_DASHBOARD_PORT,
    TRADER_NAV_ITEMS,
    analyze_wallet_details,
    create_trader_dashboard,
    generate_insight_report,
    load_alpha_leaderboard,
    load_bridge_traders,
    load_connected_traders,
    load_network_explorer_rows,
    load_overview_kpis,
    load_overview_recommendations,
    load_profile_categories,
    load_registry_summary,
    refresh_insights_action,
    run_discovery_action,
    run_profiling_action,
    run_trader_dashboard,
    trader_home_summary,
)
from src.web.trader_dashboard_styles import OVERVIEW_TABLE_LIMIT

WALLET_A = "0x" + "a" * 40
WALLET_B = "0x" + "b" * 40
WALLET_SOURCE = "0x" + "9" * 40


def _report(wallet: str, classification: str = "market_maker", confidence: float = 0.9, **metrics):
    base_metrics = {
        "trade_count": 120,
        "markets_traded": 540,
        "overlap_count": 186,
        "overlap_ratio": 0.38,
        "merge_count": 12,
        "redeem_count": 9,
        "buy_volume": 7200,
        "redeem_volume": 1800,
        "btc_volume": 8500,
        "eth_volume": 500,
        "sol_volume": 0,
        "other_volume": 0,
    }
    base_metrics.update(metrics)
    return {
        "wallet": wallet,
        "classification": classification,
        "confidence": confidence,
        "watch_score": 88,
        "metrics": base_metrics,
        "signals": [{"name": "high_merge_frequency"}, {"name": "high_overlap_frequency"}],
    }


def _seed_dashboard_fixtures(tmp_path: Path):
    traders_db = tmp_path / "traders.db"
    discovery_db = tmp_path / "discovery.db"
    export_dir = tmp_path / "wallets"
    export_dir.mkdir()

    save_wallet_report(_report(WALLET_A), watch_score=88, db_path=str(traders_db))
    save_wallet_report(
        _report(
            WALLET_B,
            classification="quantitative_directional",
            confidence=0.75,
            trade_count=40,
            markets_traded=8,
            overlap_count=12,
            overlap_ratio=0.05,
            merge_count=0,
            redeem_count=0,
            btc_volume=900,
            eth_volume=100,
        ),
        watch_score=55,
        db_path=str(traders_db),
    )
    save_discovery_candidate(TraderDiscoveryCandidate(WALLET_A, "co_market_activity", 90, 186), db_path=discovery_db)
    save_discovery_candidate(TraderDiscoveryCandidate(WALLET_B, "co_market_activity", 70, 12), db_path=discovery_db)
    save_discovery_relationships(
        WALLET_SOURCE,
        [
            TraderDiscoveryCandidate(WALLET_A, "co_market_activity", 90, 186, ["market-1", "market-2"]),
            TraderDiscoveryCandidate(WALLET_B, "co_market_activity", 70, 12, ["market-3"]),
        ],
        db_path=discovery_db,
    )
    return traders_db, discovery_db, export_dir


def test_trader_home_summary(tmp_path):
    traders_db, discovery_db, export_dir = _seed_dashboard_fixtures(tmp_path)

    summary = trader_home_summary(
        seed_wallet=WALLET_SOURCE,
        traders_db_path=traders_db,
        discovery_db_path=discovery_db,
        wallet_export_dir=export_dir,
    )

    assert summary["wallets_discovered"] == 2
    assert summary["network_nodes"] >= 2
    assert summary["network_edges"] > 0
    assert summary["profiles_generated"] == 2


def test_registry_summary_loading(tmp_path):
    traders_db, discovery_db, export_dir = _seed_dashboard_fixtures(tmp_path)

    summary = load_registry_summary(traders_db_path=traders_db)

    assert summary["traders_profiled"] == 2
    assert summary["alpha_reports"] == 2
    assert summary["market_makers"] == 1


def test_overview_kpis_loading(tmp_path):
    traders_db, discovery_db, export_dir = _seed_dashboard_fixtures(tmp_path)

    kpis = load_overview_kpis(
        seed_wallet=WALLET_SOURCE,
        traders_db_path=traders_db,
        discovery_db_path=discovery_db,
        wallet_export_dir=export_dir,
    )

    assert kpis["top_alpha"] > 0
    assert kpis["traders_profiled"] == 2
    assert kpis["discovery_candidates"] == 2


def test_alpha_leaderboard_loading(tmp_path):
    traders_db, discovery_db, export_dir = _seed_dashboard_fixtures(tmp_path)

    rows = load_alpha_leaderboard(
        limit=5,
        traders_db_path=traders_db,
        discovery_db_path=discovery_db,
    )

    assert rows
    assert rows[0]["alpha"] >= rows[-1]["alpha"]
    assert "profile" in rows[0]
    assert "classification" in rows[0]
    assert "..." in rows[0]["wallet"]


def test_connected_traders_loading(tmp_path):
    traders_db, discovery_db, export_dir = _seed_dashboard_fixtures(tmp_path)

    rows = load_connected_traders(
        seed_wallet=WALLET_SOURCE,
        limit=5,
        traders_db_path=traders_db,
        discovery_db_path=discovery_db,
        wallet_export_dir=export_dir,
    )

    assert rows
    assert "degree" in rows[0]
    assert "profile" in rows[0]


def test_bridge_traders_loading(tmp_path):
    traders_db, discovery_db, export_dir = _seed_dashboard_fixtures(tmp_path)

    rows = load_bridge_traders(
        seed_wallet=WALLET_SOURCE,
        limit=5,
        traders_db_path=traders_db,
        discovery_db_path=discovery_db,
        wallet_export_dir=export_dir,
    )

    assert isinstance(rows, list)


def test_network_explorer_rows(tmp_path):
    traders_db, discovery_db, export_dir = _seed_dashboard_fixtures(tmp_path)

    rows = load_network_explorer_rows(
        seed_wallet=WALLET_SOURCE,
        limit=5,
        traders_db_path=traders_db,
        discovery_db_path=discovery_db,
        wallet_export_dir=export_dir,
    )

    assert rows
    assert {"wallet", "degree", "centrality", "cluster", "alpha"} <= set(rows[0])


def test_profile_categories_loading(tmp_path):
    traders_db, discovery_db, export_dir = _seed_dashboard_fixtures(tmp_path)

    categories = load_profile_categories(
        seed_wallet=WALLET_SOURCE,
        limit=5,
        traders_db_path=traders_db,
        discovery_db_path=discovery_db,
        wallet_export_dir=export_dir,
    )

    assert set(categories) == {
        "btc_specialists",
        "directional_traders",
        "market_makers",
        "arbitrage_traders",
    }


def test_overview_recommendations_loading(tmp_path):
    traders_db, discovery_db, export_dir = _seed_dashboard_fixtures(tmp_path)

    rows = load_overview_recommendations(
        seed_wallet=WALLET_SOURCE,
        limit=OVERVIEW_TABLE_LIMIT,
        traders_db_path=traders_db,
        discovery_db_path=discovery_db,
        wallet_export_dir=export_dir,
    )

    assert rows
    assert {"wallet", "reason", "alpha", "shared_markets"} <= set(rows[0])


def test_wallet_search_analysis(tmp_path):
    traders_db, discovery_db, export_dir = _seed_dashboard_fixtures(tmp_path)

    details = analyze_wallet_details(
        WALLET_A,
        scan_if_missing=False,
        traders_db_path=traders_db,
        discovery_db_path=discovery_db,
        wallet_export_dir=export_dir,
    )

    assert details["wallet"] == WALLET_A
    assert details["alpha_score"] >= 70
    assert details["profile"] == "btc specialist"
    assert details["btc_volume"] == 8500


def test_insight_report_generation(tmp_path):
    traders_db, discovery_db, export_dir = _seed_dashboard_fixtures(tmp_path)

    report = generate_insight_report(
        WALLET_SOURCE,
        limit=3,
        traders_db_path=traders_db,
        discovery_db_path=discovery_db,
        wallet_export_dir=export_dir,
    )

    assert report["seed_wallet"] == WALLET_SOURCE
    assert report["recommended_traders"]
    assert report["recommended_traders"][0]["reason"]


def test_refresh_insights_action(tmp_path):
    traders_db, discovery_db, export_dir = _seed_dashboard_fixtures(tmp_path)

    refreshed = refresh_insights_action(
        WALLET_SOURCE,
        limit=3,
        traders_db_path=traders_db,
        discovery_db_path=discovery_db,
        wallet_export_dir=export_dir,
    )

    assert refreshed["seed_wallet"] == WALLET_SOURCE
    assert refreshed["recommended_traders"]


def test_refresh_actions(tmp_path, monkeypatch):
    traders_db, discovery_db, export_dir = _seed_dashboard_fixtures(tmp_path)

    monkeypatch.setattr(
        "src.web.trader_dashboard.discovery_report",
        lambda **kwargs: {"wallets_discovered": 2, "new_wallets": 1},
    )
    discovery = run_discovery_action(
        WALLET_SOURCE,
        traders_db_path=traders_db,
        discovery_db_path=discovery_db,
    )
    assert discovery["wallets_discovered"] == 2

    profiling = run_profiling_action(
        seed_wallet=WALLET_SOURCE,
        limit=2,
        traders_db_path=traders_db,
        discovery_db_path=discovery_db,
        wallet_export_dir=export_dir,
    )
    assert profiling["profiles_generated"] >= 1


def test_create_trader_dashboard_registers_page(monkeypatch):
    pages: list[str] = []

    class FakeUI:
        @staticmethod
        def page(path: str):
            def decorator(func):
                pages.append(path)
                return func

            return decorator

        @staticmethod
        def add_head_html(*args, **kwargs):
            return None

        @staticmethod
        def column(*args, **kwargs):
            return FakeElement()

        @staticmethod
        def row(*args, **kwargs):
            return FakeElement()

        @staticmethod
        def label(*args, **kwargs):
            return FakeElement()

        @staticmethod
        def button(*args, **kwargs):
            return FakeElement()

        @staticmethod
        def input(*args, **kwargs):
            return FakeElement()

        @staticmethod
        def element(*args, **kwargs):
            return FakeElement()

        @staticmethod
        def table(*args, **kwargs):
            return FakeElement()

        @staticmethod
        def notify(*args, **kwargs):
            return None

    class FakeElement:
        def classes(self, *args, **kwargs):
            return self

        def props(self, *args, **kwargs):
            return self

        def bind_value(self, *args, **kwargs):
            return self

        def clear(self):
            return None

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setitem(sys.modules, "nicegui", type(sys)("nicegui"))
    sys.modules["nicegui"].ui = FakeUI
    create_trader_dashboard()
    assert pages == ["/"]


def test_trader_dashboard_cli_defaults(monkeypatch):
    calls: list[tuple[str | None, int | None]] = []

    def fake_run_trader_dashboard(*, host: str | None = None, port: int | None = None, config=None) -> None:
        calls.append((host, port))

    monkeypatch.setattr("src.web.trader_dashboard.run_trader_dashboard", fake_run_trader_dashboard)
    monkeypatch.setattr(sys, "argv", ["polylens", "trader-dashboard"])

    from src.cli import main

    main()
    assert calls == [(DEFAULT_TRADER_DASHBOARD_HOST, DEFAULT_TRADER_DASHBOARD_PORT)]


def test_trader_nav_items_cover_required_pages():
    assert TRADER_NAV_ITEMS == ("Overview", "Network", "Profiles", "Signals", "Discovery", "Performance", "Service", "Insights")


def test_load_wallet_signal_dashboard_returns_pipeline(tmp_path):
    from src.web.trader_dashboard import load_wallet_signal_dashboard

    data = load_wallet_signal_dashboard(
        traders_db_path=tmp_path / "traders.db",
        signal_db_path=tmp_path / "signals.db",
        paper_copy_db_path=tmp_path / "paper_copy.db",
        limit=5,
    )
    assert "pipeline" in data
    assert "top_copied_wallets" in data
    assert "archetype_performance" in data


def test_load_wallet_discovery_dashboard_returns_sections(tmp_path):
    from src.web.trader_dashboard import load_wallet_discovery_dashboard

    data = load_wallet_discovery_dashboard(
        traders_db_path=tmp_path / "traders.db",
        discovery_db_path=tmp_path / "discovery.db",
        limit=5,
    )
    assert "analytics" in data
    assert "top_ranked" in data
    assert "recent_discoveries" in data


def test_load_wallet_performance_dashboard_returns_sections(tmp_path):
    from src.web.trader_dashboard import load_wallet_performance_dashboard

    data = load_wallet_performance_dashboard(
        traders_db_path=tmp_path / "traders.db",
        discovery_db_path=tmp_path / "discovery.db",
        limit=5,
    )
    assert "analytics" in data
    assert "top_performers" in data
    assert "promotions" in data
    assert "roi_distribution" in data


def test_load_wallet_service_dashboard_returns_sections(tmp_path):
    from src.web.trader_dashboard import load_wallet_service_dashboard

    data = load_wallet_service_dashboard(
        traders_db_path=tmp_path / "traders.db",
        discovery_db_path=tmp_path / "discovery.db",
        limit=5,
    )
    assert "health" in data
    assert "status" in data
    assert "cycle_history" in data
    assert "discovery_status" in data
