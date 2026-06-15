from __future__ import annotations

import json
from io import StringIO
import sys
from pathlib import Path

import pytest

from src.analysis.trader_registry import list_traders
from src.intelligence.wallet_autonomy_service import CYCLE_NAMES, WalletAutonomyService
from src.intelligence.wallet_seed_import import (
    WalletSeedImporter,
    bootstrap_health_report,
    ecosystem_is_empty,
    run_wallet_bootstrap_cycle,
)
from src.intelligence.wallet_synthetic_filter import is_synthetic_wallet

ROOT = Path(__file__).resolve().parents[1]
REAL_SEED = ROOT / "data" / "traders" / "real_seed_wallets.json"
SEED_EXPORTS = ROOT / "data" / "traders" / "seed_exports"
FIXTURES = ROOT / "tests" / "fixtures" / "wallet_forensics"
REAL_WALLET = "0x927f7694de44d19a72bce76254e628d1c141d215"
SYNTHETIC_A = "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"


@pytest.fixture
def empty_dbs(tmp_path):
    traders_db = tmp_path / "traders.db"
    discovery_db = tmp_path / "discovery.db"
    watchlist = tmp_path / "watchlist.json"
    seed_exports = tmp_path / "seed_exports"
    seed_exports.mkdir()
    for path in SEED_EXPORTS.glob("*.json"):
        seed_exports.joinpath(path.name).write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    real_seed = tmp_path / "real_seed_wallets.json"
    real_seed.write_text(REAL_SEED.read_text(encoding="utf-8"), encoding="utf-8")
    fixture_exports = tmp_path / "fixture_exports"
    fixture_exports.mkdir()
    for path in FIXTURES.glob("*.json"):
        fixture_exports.joinpath(path.name).write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    return {
        "traders_db": traders_db,
        "discovery_db": discovery_db,
        "watchlist": watchlist,
        "export_dir": tmp_path / "wallets",
        "seed_exports": seed_exports,
        "fixture_exports": fixture_exports,
        "real_seed": real_seed,
    }


def _bootstrap_kwargs(empty_dbs: dict) -> dict:
    return {
        "traders_db_path": empty_dbs["traders_db"],
        "discovery_db_path": empty_dbs["discovery_db"],
        "watchlist_path": empty_dbs["watchlist"],
        "export_dir": empty_dbs["export_dir"],
        "seed_wallets_path": empty_dbs["real_seed"],
        "seed_exports_dir": empty_dbs["seed_exports"],
    }


def test_ecosystem_is_empty_on_fresh_db(empty_dbs):
    assert ecosystem_is_empty(
        traders_db_path=empty_dbs["traders_db"],
        discovery_db_path=empty_dbs["discovery_db"],
    )


def test_seed_import_real_wallet_only(empty_dbs):
    importer = WalletSeedImporter(
        traders_db_path=empty_dbs["traders_db"],
        discovery_db_path=empty_dbs["discovery_db"],
        watchlist_path=empty_dbs["watchlist"],
        export_dir=empty_dbs["export_dir"],
        seed_wallets_path=empty_dbs["real_seed"],
        seed_exports_dir=empty_dbs["seed_exports"],
    )
    result = importer.bootstrap_from_packaged_seeds()
    assert result["imported_count"] >= 1
    rows = list_traders(limit=10, db_path=str(empty_dbs["traders_db"]))
    assert len(rows) >= 1
    assert all(not is_synthetic_wallet(row.wallet) for row in rows)
    assert empty_dbs["watchlist"].exists()


def test_synthetic_fixture_import_skipped(empty_dbs):
    importer = WalletSeedImporter(
        traders_db_path=empty_dbs["traders_db"],
        discovery_db_path=empty_dbs["discovery_db"],
        watchlist_path=empty_dbs["watchlist"],
        seed_exports_dir=empty_dbs["fixture_exports"],
    )
    results = importer.import_directory_exports()
    imported = [row for row in results if row.imported]
    skipped = [row for row in results if row.skipped]
    assert len(imported) == 1
    assert len(skipped) >= 2
    assert any("synthetic wallet" in (row.reason or "") for row in skipped)


def test_bootstrap_cycle_populates_ecosystem(empty_dbs):
    before = bootstrap_health_report(
        traders_db_path=empty_dbs["traders_db"],
        discovery_db_path=empty_dbs["discovery_db"],
    )
    assert before["registry_population"] == 0
    assert before["real_wallet_count"] == 0

    result = run_wallet_bootstrap_cycle(**_bootstrap_kwargs(empty_dbs))
    assert result["skipped"] is False
    health = result["health"]
    assert health["registry_population"] > 0
    assert health["real_wallet_count"] > 0
    assert health["discovery_population"] > 0
    assert health["wallet_acquisition_records"] > 0


def test_bootstrap_skips_when_not_empty(empty_dbs):
    run_wallet_bootstrap_cycle(**_bootstrap_kwargs(empty_dbs))
    skipped = run_wallet_bootstrap_cycle(**_bootstrap_kwargs(empty_dbs))
    assert skipped["skipped"] is True


def test_bootstrap_deduplicates_on_restart(empty_dbs):
    importer = WalletSeedImporter(
        traders_db_path=empty_dbs["traders_db"],
        discovery_db_path=empty_dbs["discovery_db"],
        watchlist_path=empty_dbs["watchlist"],
        export_dir=empty_dbs["export_dir"],
        seed_wallets_path=empty_dbs["real_seed"],
        seed_exports_dir=empty_dbs["seed_exports"],
    )
    first = importer.bootstrap_from_packaged_seeds()
    second = importer.bootstrap_from_packaged_seeds()
    assert first["imported_count"] >= 1
    assert second["imported_count"] == 0
    assert second["skipped_duplicates"] >= 1


def test_autonomy_bootstrap_cycle(empty_dbs, monkeypatch):
    import src.intelligence.wallet_seed_import as seed_module

    monkeypatch.setattr(seed_module, "DEFAULT_REAL_SEED_WALLETS_PATH", str(empty_dbs["real_seed"]))
    monkeypatch.setattr(seed_module, "DEFAULT_SEED_EXPORTS_DIR", str(empty_dbs["seed_exports"]))
    monkeypatch.setattr(seed_module, "DEFAULT_WALLET_EXPORT_DIR", str(empty_dbs["export_dir"]))
    service = WalletAutonomyService(
        traders_db_path=empty_dbs["traders_db"],
        discovery_db_path=empty_dbs["discovery_db"],
    )
    result = service.run_bootstrap_cycle()
    assert result["status"] == "success"
    payload = result["result"]
    assert payload.get("skipped") is False
    assert payload["health"]["real_wallet_count"] > 0


def test_bootstrap_in_cycle_names():
    assert "bootstrap" in CYCLE_NAMES


def test_wallet_bootstrap_cli_json(empty_dbs):
    from src.cli import wallet_bootstrap_cli

    result = wallet_bootstrap_cli(
        as_json=True,
        traders_db_path=str(empty_dbs["traders_db"]),
        discovery_db_path=str(empty_dbs["discovery_db"]),
    )
    assert result["read_only"] is True
    assert result["health"]["real_wallet_count"] > 0


def test_wallet_bootstrap_health_cli_human(empty_dbs, monkeypatch):
    from src.cli import wallet_bootstrap_health_cli

    run_wallet_bootstrap_cycle(**_bootstrap_kwargs(empty_dbs))
    captured = StringIO()
    monkeypatch.setattr(sys, "stdout", captured)
    wallet_bootstrap_health_cli(
        as_json=False,
        traders_db_path=str(empty_dbs["traders_db"]),
        discovery_db_path=str(empty_dbs["discovery_db"],
        ),
    )
    assert "real=" in captured.getvalue()


def test_wallet_seed_import_cli(empty_dbs):
    from src.cli import wallet_seed_import_cli

    result = wallet_seed_import_cli(
        as_json=True,
        traders_db_path=str(empty_dbs["traders_db"]),
        discovery_db_path=str(empty_dbs["discovery_db"]),
        exports_dir=str(empty_dbs["seed_exports"]),
        seed_path=str(empty_dbs["real_seed"]),
    )
    assert result["imported_count"] >= 1
