from __future__ import annotations

import hashlib
import shutil
import sqlite3
from pathlib import Path

import pytest

from src.analysis.paper_trading_engine import DEFAULT_PAPER_TRADING_DB, init_paper_trading_db
from src.analysis.trader_registry import DEFAULT_TRADERS_DB, save_wallet_report
from src.analysis.wallet_activity import DEFAULT_WALLET_ACTIVITY_DB, WalletActivityExport, save_wallet_activity_export
from src.intelligence.wallet_baseline_analysis import wallet_baseline_analysis_report
from src.intelligence.wallet_scoring import WalletScorer
from src.intelligence.wallet_signal_analytics import _wallet_validation_stats
from src.testing.runtime_db_guard import (
    MISSING,
    RUNTIME_DB_FILES,
    compare_snapshots,
    format_validation_report,
    validate_runtime_db_isolation,
)
from src.testing.runtime_db_redirect import PRODUCTION_RUNTIME_DBS, runtime_db_redirect_active

pytestmark = pytest.mark.runtime_db_safe

PRODUCTION_TRADER_SIGNALS_DB = PRODUCTION_RUNTIME_DBS["trader_signals"]

WALLET = "0x" + "a" * 40


def _report():
    return {
        "wallet": WALLET,
        "classification": "arbitrage_trader",
        "confidence": 0.9,
        "watch_score": 88,
        "metrics": {"trade_count": 50, "markets_traded": 20, "overlap_ratio": 0.35},
        "signals": [],
    }


def _make_stale_signal_db(path: Path) -> None:
    if PRODUCTION_TRADER_SIGNALS_DB.exists():
        shutil.copy(PRODUCTION_TRADER_SIGNALS_DB, path)
    else:
        sqlite3.connect(path).close()
    conn = sqlite3.connect(path)
    conn.execute("DROP TABLE IF EXISTS trader_signal_validation")
    conn.execute("DROP VIEW IF EXISTS v_validation_trend")
    conn.commit()
    conn.close()


def test_wallet_validation_stats_repairs_stale_signal_db(tmp_path):
    stale_db = tmp_path / "stale_signals.db"
    _make_stale_signal_db(stale_db)

    stats = _wallet_validation_stats(stale_db)

    assert stats == {}
    conn = sqlite3.connect(stale_db)
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    conn.close()
    assert "trader_signal_validation" in tables


def test_wallet_scorer_uses_isolated_signal_db(signal_db_path, tmp_path):
    traders_db = tmp_path / "traders.db"
    save_wallet_report(_report(), db_path=str(traders_db))

    scorer = WalletScorer(
        traders_db_path=traders_db,
        discovery_db_path=tmp_path / "discovery.db",
        signal_db_path=signal_db_path,
        paper_copy_db_path=tmp_path / "paper_copy.db",
        wallet_activity_db_path=tmp_path / "wallet_activity.db",
    )
    score = scorer.score_wallet(WALLET)

    assert score.wallet == WALLET
    assert signal_db_path.resolve() != PRODUCTION_TRADER_SIGNALS_DB


def test_baseline_analysis_uses_isolated_signal_db(signal_db_path, tmp_path):
    traders_db = tmp_path / "traders.db"
    discovery_db = tmp_path / "discovery.db"
    save_wallet_report(_report(), db_path=str(traders_db))

    result = wallet_baseline_analysis_report(
        traders_db_path=str(traders_db),
        discovery_db_path=str(discovery_db),
        paper_copy_db_path=tmp_path / "paper_copy.db",
        signal_db_path=str(signal_db_path),
    )

    assert result["read_only"] is True
    assert "baselines" in result


def _hash_file(path: Path) -> str | None:
    if not path.exists():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _runtime_hashes() -> dict[str, str | None]:
    return {name: _hash_file(path) for name, path in PRODUCTION_RUNTIME_DBS.items()}


def _table_names(path: Path) -> set[str]:
    conn = sqlite3.connect(path)
    try:
        return {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()


def test_runtime_db_redirect_hook_active_during_tests():
    assert runtime_db_redirect_active() is True


def test_default_trader_signal_db_path_redirects_to_isolated_file(isolated_runtime_db_paths):
    isolated_path = isolated_runtime_db_paths["trader_signals"]
    assert not isolated_path.exists()

    _wallet_validation_stats(str(PRODUCTION_TRADER_SIGNALS_DB))

    assert isolated_path.exists()
    assert isolated_path.resolve() != PRODUCTION_TRADER_SIGNALS_DB
    assert "trader_signal_validation" in _table_names(isolated_path)


def test_default_runtime_db_paths_redirect_to_isolated_files(isolated_runtime_db_paths):
    before = _runtime_hashes()

    init_paper_trading_db(DEFAULT_PAPER_TRADING_DB)
    _wallet_validation_stats(str(PRODUCTION_RUNTIME_DBS["trader_signals"]))
    save_wallet_report(_report(), db_path=DEFAULT_TRADERS_DB)
    save_wallet_activity_export(
        WalletActivityExport(
            wallet=WALLET,
            export_timestamp="2026-06-26T00:00:00Z",
            source="test",
            event_count=0,
            events=[],
        ),
        db_path=DEFAULT_WALLET_ACTIVITY_DB,
    )

    assert _runtime_hashes() == before
    assert "paper_positions" in _table_names(isolated_runtime_db_paths["paper_trading"])
    assert "trader_signal_validation" in _table_names(isolated_runtime_db_paths["trader_signals"])
    assert "wallet_reports" in _table_names(isolated_runtime_db_paths["traders"])
    assert "wallet_exports" in _table_names(isolated_runtime_db_paths["wallet_activity"])


def test_runtime_db_hashes_unchanged_after_representative_default_path_run(isolated_runtime_db_paths):
    before = _runtime_hashes()

    init_paper_trading_db()
    _wallet_validation_stats("data/trader_signals.db")
    save_wallet_report(_report())
    save_wallet_activity_export(
        WalletActivityExport(
            wallet=WALLET,
            export_timestamp="2026-06-26T00:00:00Z",
            source="regression-guard",
            event_count=0,
            events=[],
        )
    )

    assert _runtime_hashes() == before
    assert all(path.exists() for path in isolated_runtime_db_paths.values())


def test_wallet_scoring_stale_closing_connection_import_bypass(isolated_runtime_db_paths, tmp_path):
    """Regression for modules that keep a module-level closing_connection import."""
    import src.intelligence.wallet_scoring as wallet_scoring

    traders_db = tmp_path / "traders.db"
    save_wallet_report(_report(), db_path=str(traders_db))
    before = _runtime_hashes()

    with wallet_scoring.closing_connection(DEFAULT_TRADERS_DB) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS bypass_probe (id INTEGER PRIMARY KEY)")

    assert _runtime_hashes() == before
    assert "bypass_probe" in _table_names(isolated_runtime_db_paths["traders"])


def test_mission_control_default_portfolio_path_redirects_without_mutation(isolated_runtime_db_paths, monkeypatch):
    """Regression: mission_control_snapshot defaults portfolio_db_path to data/paper_trading.db."""
    import sys
    import types

    from src.web.mission_control_data import mission_control_snapshot

    if not PRODUCTION_RUNTIME_DBS["paper_trading"].exists():
        pytest.skip("production paper_trading.db absent")

    before = _runtime_hashes()
    monkeypatch.setattr(
        "src.web.mission_control_data._legacy_mission_control_snapshot",
        lambda **_kwargs: {"header": {}, "kpis": {}, "generated_at": "2026-06-26T00:00:00Z"},
    )
    fake_execution = types.ModuleType("src.analysis.paper_execution_analytics")
    fake_execution.execution_quality_report = lambda portfolio_db_path, **kwargs: {}
    monkeypatch.setitem(sys.modules, "src.analysis.paper_execution_analytics", fake_execution)
    monkeypatch.setattr(
        "src.analysis.opportunity_feed.get_paper_trading_opportunities",
        lambda **kwargs: [],
    )
    monkeypatch.setattr(
        "src.analysis.outcome_validator.outcome_validation_report",
        lambda portfolio_db_path, **kwargs: {},
    )
    monkeypatch.setattr(
        "src.analysis.signal_weight_report.build_signal_weight_report",
        lambda portfolio_db_path, **kwargs: {},
    )
    monkeypatch.setattr(
        "src.analysis.signal_weight_report.mission_control_section",
        lambda report, **kwargs: {},
    )

    mission_control_snapshot()

    assert _runtime_hashes() == before
    assert isolated_runtime_db_paths["paper_trading"].exists()


def test_sqlite3_connect_redirects_default_paths(isolated_runtime_db_paths):
    before = _runtime_hashes()
    conn = sqlite3.connect(DEFAULT_TRADERS_DB)
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS connect_probe (id INTEGER PRIMARY KEY)")
        conn.commit()
    finally:
        conn.close()

    assert _runtime_hashes() == before
    assert "connect_probe" in _table_names(isolated_runtime_db_paths["traders"])


def _snapshot_with(**overrides: str) -> dict[str, str]:
    base = {name: MISSING for name in RUNTIME_DB_FILES}
    base.update(overrides)
    return base


def test_validator_passes_when_snapshots_match():
    snapshot = _snapshot_with(
        **{
            "trader_signals.db": "abc123",
            "traders.db": "def456",
            "wallet_activity.db": "ghi789",
            "paper_trading.db": "jkl012",
        }
    )
    failures = compare_snapshots(snapshot, snapshot)
    report = format_validation_report(snapshot, snapshot, failures)

    assert not failures
    assert "PASS" in report
    assert "trader_signals.db unchanged" in report


def test_validate_runtime_db_isolation_returns_workload_exit_code_when_clean():
    snapshot = _snapshot_with(
        **{
            "trader_signals.db": "abc123",
            "traders.db": "def456",
            "wallet_activity.db": "ghi789",
            "paper_trading.db": "jkl012",
        }
    )
    exit_code, report = validate_runtime_db_isolation(lambda: 0, before=snapshot, after=snapshot)

    assert exit_code == 0
    assert "PASS" in report


def test_validator_detects_modified_db_hash():
    before = _snapshot_with(**{"paper_trading.db": "abc123"})
    after = _snapshot_with(**{"paper_trading.db": "def456"})
    failures = compare_snapshots(before, after)

    assert len(failures) == 1
    assert failures[0].db_name == "paper_trading.db"
    assert "hash changed" in failures[0].message()
    report = format_validation_report(before, after, failures)
    assert "FAIL" in report
    assert "Expected:" in report
    assert "abc123" in report
    assert "def456" in report


def test_validator_detects_newly_created_runtime_db():
    before = _snapshot_with()
    after = _snapshot_with(**{"traders.db": "newfilehash"})
    failures = compare_snapshots(before, after)

    assert len(failures) == 1
    assert failures[0].kind == "created"
    assert failures[0].db_name == "traders.db"


def test_validator_detects_deleted_runtime_db():
    before = _snapshot_with(**{"wallet_activity.db": "stablehash"})
    after = _snapshot_with()
    failures = compare_snapshots(before, after)

    assert len(failures) == 1
    assert failures[0].kind == "deleted"
    assert failures[0].db_name == "wallet_activity.db"


def test_validate_runtime_db_isolation_propagates_workload_failure():
    snapshot = _snapshot_with(**{"traders.db": "stable"})
    exit_code, report = validate_runtime_db_isolation(lambda: 3, before=snapshot, after=snapshot)

    assert exit_code == 3
    assert "PASS" in report
