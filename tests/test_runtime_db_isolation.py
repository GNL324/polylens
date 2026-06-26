from __future__ import annotations

import hashlib
import shutil
import sqlite3
from pathlib import Path

import pytest

from src.analysis.paper_trading_engine import DEFAULT_PAPER_TRADING_DB, init_paper_trading_db
from src.analysis.trader_registry import DEFAULT_TRADERS_DB
from src.analysis.trader_registry import save_wallet_report
from src.analysis.wallet_activity import DEFAULT_WALLET_ACTIVITY_DB, WalletActivityExport, save_wallet_activity_export
from src.cli import outcome_validation_backfill_cli, outcome_validation_report_cli, outcome_validation_run_cli
from src.intelligence.wallet_baseline_analysis import wallet_baseline_analysis_report
from src.intelligence.wallet_scoring import WalletScorer
from src.intelligence.wallet_signal_analytics import _wallet_validation_stats

REPO_ROOT = Path(__file__).resolve().parents[1]
PRODUCTION_RUNTIME_DBS = {
    "paper_trading": (REPO_ROOT / "data" / "paper_trading.db").resolve(),
    "trader_signals": (REPO_ROOT / "data" / "trader_signals.db").resolve(),
    "traders": (REPO_ROOT / "data" / "traders.db").resolve(),
    "wallet_activity": (REPO_ROOT / "data" / "wallet_activity.db").resolve(),
}
PRODUCTION_TRADER_SIGNALS_DB = (REPO_ROOT / "data" / "trader_signals.db").resolve()

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


def test_outcome_validation_cli_commands_use_explicit_temp_db(tmp_path):
    before = _runtime_hashes()
    db_path = tmp_path / "outcome-cli.db"

    run = outcome_validation_run_cli(as_json=True, db_path=str(db_path), limit=10)
    report = outcome_validation_report_cli(as_json=True, db_path=str(db_path), window_days=7)
    backfill = outcome_validation_backfill_cli(as_json=True, db_path=str(db_path), batch_size=10, reset=True)

    assert run["paper_only"] is True
    assert report["paper_only"] is True
    assert backfill["paper_only"] is True
    assert db_path.exists()
    assert _runtime_hashes() == before
