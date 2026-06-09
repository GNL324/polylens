from __future__ import annotations

import sys

import pytest


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
