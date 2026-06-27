from __future__ import annotations

import json
from io import StringIO
import sys

from src.intelligence.wallet_autonomy_service import init_wallet_service_state_db


def test_wallet_service_status_cli_json(tmp_path, monkeypatch):
    traders_db = tmp_path / "traders.db"
    init_wallet_service_state_db(traders_db)

    monkeypatch.chdir(tmp_path)
    import src.intelligence.wallet_autonomy_service as autonomy_module

    monkeypatch.setattr(autonomy_module, "DEFAULT_TRADERS_DB", str(traders_db))
    monkeypatch.setattr(autonomy_module, "DEFAULT_TRADER_DISCOVERY_DB", str(tmp_path / "discovery.db"))

    from src.cli import wallet_service_status_cli

    captured = StringIO()
    monkeypatch.setattr(sys, "stdout", captured)
    result = wallet_service_status_cli(as_json=True)
    assert result["read_only"] is True
    payload = json.loads(captured.getvalue())
    assert "cycles" in payload


def test_wallet_service_health_cli_human(tmp_path, monkeypatch):
    traders_db = tmp_path / "traders.db"
    init_wallet_service_state_db(traders_db)

    monkeypatch.chdir(tmp_path)
    import src.intelligence.wallet_service_health as health_module

    monkeypatch.setattr(health_module, "DEFAULT_TRADERS_DB", str(traders_db))

    from src.cli import wallet_service_health_cli

    captured = StringIO()
    monkeypatch.setattr(sys, "stdout", captured)
    wallet_service_health_cli(as_json=False)
    assert "Health:" in captured.getvalue()


def test_wallet_service_run_cli_force(tmp_path, monkeypatch):
    traders_db = tmp_path / "traders.db"
    discovery_db = tmp_path / "discovery.db"
    init_wallet_service_state_db(traders_db)

    monkeypatch.chdir(tmp_path)
    import src.intelligence.wallet_autonomy_service as autonomy_module

    monkeypatch.setattr(autonomy_module, "DEFAULT_TRADERS_DB", str(traders_db))
    monkeypatch.setattr(autonomy_module, "DEFAULT_TRADER_DISCOVERY_DB", str(discovery_db))

    expected = {"read_only": True, "cycles_run": 2, "duration_ms": 12.5, "results": []}
    monkeypatch.setattr(
        autonomy_module,
        "run_wallet_autonomy_service",
        lambda force=False, traders_db_path=str(traders_db): expected,
    )

    from src.cli import wallet_service_run_cli

    result = wallet_service_run_cli(as_json=True, force=True)
    assert result["read_only"] is True
    assert result["cycles_run"] == 2
