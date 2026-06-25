from __future__ import annotations

import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def test_web_dashboard_cli_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str | None, int | None]] = []

    def fake_run_web_dashboard(*, host: str | None = None, port: int | None = None) -> None:
        calls.append((host, port))

    monkeypatch.setattr("src.web.app.run_web_dashboard", fake_run_web_dashboard)
    monkeypatch.setattr(sys, "argv", ["polylens", "web-dashboard"])

    from src.cli import main

    main()

    assert calls == [("127.0.0.1", 8787)]


def test_web_dashboard_cli_host_and_port(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str | None, int | None]] = []

    def fake_run_web_dashboard(*, host: str | None = None, port: int | None = None) -> None:
        calls.append((host, port))

    monkeypatch.setattr("src.web.app.run_web_dashboard", fake_run_web_dashboard)
    monkeypatch.setattr(sys, "argv", ["polylens", "web-dashboard", "--host", "0.0.0.0", "--port", "8787"])

    from src.cli import main

    main()

    assert calls == [("0.0.0.0", 8787)]


def test_mission_control_root_route_redirects_to_mission_control() -> None:
    source = (ROOT / "src" / "web" / "mission_control.py").read_text()

    assert '@ui.page("/")' in source
    assert 'RedirectResponse("/mission-control", status_code=307)' in source
    assert '@ui.page("/mission-control")' in source


def test_mission_control_tabs_and_responsive_grid_are_present() -> None:
    source = (ROOT / "src" / "web" / "mission_control.py").read_text()
    css = (ROOT / "src" / "web" / "mission_control_styles.py").read_text()

    for label in ("Overview", "Opportunities", "Wallets", "Signals", "Strategies", "System Health"):
        assert f'ui.tab("{label}")' in source

    assert "mc-dashboard-grid mc-overview-grid" in source
    assert "mc-span-8" in source
    assert "mc-span-4" in source
    assert "mc-balanced-grid" in source
    assert "grid-template-columns: repeat(12, minmax(0, 1fr))" in css
    assert "grid-template-columns: repeat(6, minmax(0, 1fr))" in css
    assert "grid-template-columns: 1fr" in css
    assert ".mc-shell" in css
    assert "width: 100%;" in css


def test_dashboard_exposes_paper_portfolio_views() -> None:
    source = (ROOT / "src" / "web" / "dashboard.py").read_text()

    for label in (
        "Paper Portfolio",
        "Equity Curve",
        "Trade History",
        "Wallet Performance",
        "Strategy Performance",
        "PnL Attribution",
        "Capital Allocation",
        "Recent Trades",
    ):
        assert label in source
    assert "portfolio_report" in source
