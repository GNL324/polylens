from __future__ import annotations

import json

from src.analysis.trader_registry import save_wallet_report
from src.intelligence.wallet_alpha_lab import WalletAlphaLab, init_wallet_alpha_lab_db
from src.intelligence.wallet_autonomy_service import CYCLE_NAMES, WalletAutonomyService
from src.intelligence.wallet_baseline_analysis import wallet_baseline_analysis_report
from src.intelligence.wallet_signal_decay import wallet_decay_report
from src.intelligence.wallet_signal_decay import wallet_decay_report

WALLET = "0x" + "a" * 40


def _report():
    return {
        "wallet": WALLET,
        "classification": "arbitrage_trader",
        "confidence": 0.85,
        "watch_score": 80,
        "metrics": {"trade_count": 50, "markets_traded": 20, "overlap_ratio": 0.35},
        "signals": [],
    }


def test_analyze_wallet_returns_alpha_report(tmp_path):
    traders_db = tmp_path / "traders.db"
    discovery_db = tmp_path / "discovery.db"
    save_wallet_report(_report(), db_path=str(traders_db))
    init_wallet_alpha_lab_db(traders_db, discovery_db)
    lab = WalletAlphaLab(traders_db_path=traders_db, discovery_db_path=discovery_db)
    report = lab.analyze_wallet(WALLET)
    assert report.wallet == WALLET
    assert 0 <= report.alpha_score <= 100
    assert report.status in {"strong", "developing", "weak", "insufficient_data"}


def test_compute_alpha_score_grade(tmp_path):
    traders_db = tmp_path / "traders.db"
    discovery_db = tmp_path / "discovery.db"
    save_wallet_report(_report(), db_path=str(traders_db))
    lab = WalletAlphaLab(traders_db_path=traders_db, discovery_db_path=discovery_db)
    report = lab.analyze_wallet(WALLET)
    score = lab.compute_alpha_score(report)
    assert score.alpha_grade in {"A", "B", "C", "D", "F"}
    assert "performance" in score.factors


def test_baseline_analysis_report(tmp_path, signal_db_path):
    traders_db = tmp_path / "traders.db"
    discovery_db = tmp_path / "discovery.db"
    init_wallet_alpha_lab_db(traders_db, discovery_db)
    save_wallet_report(_report(), db_path=str(traders_db))
    result = wallet_baseline_analysis_report(
        traders_db_path=str(traders_db),
        discovery_db_path=str(discovery_db),
        paper_copy_db_path=tmp_path / "paper_copy.db",
        signal_db_path=str(signal_db_path),
    )
    assert result["read_only"] is True
    assert "baselines" in result


def test_decay_report_empty_db(tmp_path):
    traders_db = tmp_path / "traders.db"
    result = wallet_decay_report(traders_db_path=str(traders_db))
    assert result["read_only"] is True
    assert "wallets" in result


def test_promotion_validation(tmp_path):
    traders_db = tmp_path / "traders.db"
    discovery_db = tmp_path / "discovery.db"
    init_wallet_alpha_lab_db(traders_db, discovery_db)
    save_wallet_report(_report(), db_path=str(traders_db))
    lab = WalletAlphaLab(traders_db_path=traders_db, discovery_db_path=discovery_db)
    validation = lab.promotion_validation()
    assert "promotion_justified" in validation
    assert "demotion_justified" in validation


def test_run_alpha_lab_cycle(tmp_path):
    traders_db = tmp_path / "traders.db"
    discovery_db = tmp_path / "discovery.db"
    save_wallet_report(_report(), db_path=str(traders_db))
    lab = WalletAlphaLab(traders_db_path=traders_db, discovery_db_path=discovery_db)
    result = lab.run_alpha_lab_cycle(limit=5)
    assert result["read_only"] is True
    assert "research_answers" in result
    assert "promoted_outperform_average" in result["research_answers"]


def test_autonomy_alpha_cycle(tmp_path):
    traders_db = tmp_path / "traders.db"
    discovery_db = tmp_path / "discovery.db"
    save_wallet_report(_report(), db_path=str(traders_db))
    service = WalletAutonomyService(traders_db_path=traders_db, discovery_db_path=discovery_db)
    result = service.run_alpha_cycle()
    assert result["cycle"] == "alpha"
    assert "alpha" in CYCLE_NAMES
