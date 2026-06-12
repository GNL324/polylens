from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import src.analysis.strategy_recommendations as recommendations
from src.analysis.strategy_recommendations import strategy_recommendations_report


def test_recommendation_rules(monkeypatch) -> None:
    monkeypatch.setattr(
        recommendations,
        "strategy_feedback_report",
        lambda db_path, *, min_trades, max_adjustment: {
            "status": "ok",
            "db_path": db_path,
            "min_trades": min_trades,
            "max_adjustment": max_adjustment,
            "strategies": [
                {
                    "strategy_label": "too_small",
                    "closed_trades": 6,
                    "avg_roi": 0.30,
                    "eligible": False,
                    "score_adjustment": 0.0,
                    "reason": "needs at least 25 closed trades",
                },
                {
                    "strategy_label": "positive",
                    "closed_trades": 25,
                    "avg_roi": 0.04,
                    "eligible": True,
                    "score_adjustment": 0.04,
                },
                {
                    "strategy_label": "negative",
                    "closed_trades": 100,
                    "avg_roi": -0.03,
                    "eligible": True,
                    "score_adjustment": -0.03,
                },
                {
                    "strategy_label": "flat",
                    "closed_trades": 40,
                    "avg_roi": 0.0,
                    "eligible": True,
                    "score_adjustment": 0.0,
                },
            ],
        },
    )

    report = strategy_recommendations_report("paper.db", min_trades=25, max_adjustment=0.10)
    by_label = {row["strategy_label"]: row for row in report["recommendations"]}

    assert by_label["too_small"]["recommendation"] == "hold"
    assert by_label["too_small"]["confidence"] == "low"
    assert by_label["too_small"]["reason"] == "needs at least 25 closed trades"
    assert by_label["positive"]["recommendation"] == "increase_trust"
    assert by_label["positive"]["confidence"] == "medium"
    assert by_label["positive"]["reason"] == "positive ROI over sufficient sample size"
    assert by_label["negative"]["recommendation"] == "decrease_trust"
    assert by_label["negative"]["confidence"] == "high"
    assert by_label["negative"]["reason"] == "negative ROI over sufficient sample size"
    assert by_label["flat"]["recommendation"] == "hold"
    assert by_label["flat"]["confidence"] == "medium"


def test_recommendations_cli_json(tmp_path: Path) -> None:
    db_path = tmp_path / "recommendations.db"
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE paper_trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                strategy_label TEXT
            );
            CREATE TABLE paper_settlements (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trade_id INTEGER NOT NULL,
                pnl REAL,
                roi REAL
            );
            """
        )
        for _ in range(25):
            cur = conn.execute("INSERT INTO paper_trades (strategy_label) VALUES ('cli_positive')")
            conn.execute("INSERT INTO paper_settlements (trade_id, pnl, roi) VALUES (?, 1.0, 0.04)", (cur.lastrowid,))

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "src.cli",
            "strategy-recommendations",
            "--db-path",
            str(db_path),
            "--json",
        ],
        cwd=Path(__file__).resolve().parents[1],
        check=True,
        capture_output=True,
        text=True,
    )

    payload = json.loads(result.stdout)
    assert payload["status"] == "ok"
    assert payload["recommendations"] == [
        {
            "strategy_label": "cli_positive",
            "closed_trades": 25,
            "avg_roi": 0.04,
            "score_adjustment": 0.04,
            "recommendation": "increase_trust",
            "confidence": "medium",
            "reason": "positive ROI over sufficient sample size",
        }
    ]
