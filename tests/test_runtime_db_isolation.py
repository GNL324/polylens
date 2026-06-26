from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

import pytest

from src.analysis.trader_registry import save_wallet_report
from src.intelligence.wallet_baseline_analysis import wallet_baseline_analysis_report
from src.intelligence.wallet_scoring import WalletScorer
from src.intelligence.wallet_signal_analytics import _wallet_validation_stats

REPO_ROOT = Path(__file__).resolve().parents[1]
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


def test_default_trader_signal_db_path_redirects_to_isolated_file(_isolate_production_trader_signals_db):
    isolated_path = _isolate_production_trader_signals_db
    assert not isolated_path.exists()

    _wallet_validation_stats(str(PRODUCTION_TRADER_SIGNALS_DB))

    assert isolated_path.exists()
    assert isolated_path.resolve() != PRODUCTION_TRADER_SIGNALS_DB
    conn = sqlite3.connect(isolated_path)
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    conn.close()
    assert "trader_signal_validation" in tables
