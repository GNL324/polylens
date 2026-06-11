from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from src.analysis.strategy_feedback import strategy_feedback_report


def _init_db(path: Path) -> None:
    with sqlite3.connect(path) as conn:
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


def _trade(conn: sqlite3.Connection, strategy_label: str | None, pnl: float, roi: float) -> None:
    cursor = conn.execute(
        "INSERT INTO paper_trades (strategy_label) VALUES (?)",
        (strategy_label,),
    )
    conn.execute(
        "INSERT INTO paper_settlements (trade_id, pnl, roi) VALUES (?, ?, ?)",
        (cursor.lastrowid, pnl, roi),
    )


def test_strategy_feedback_missing_db(tmp_path: Path) -> None:
    report = strategy_feedback_report(str(tmp_path / "missing.db"))

    assert report == {
        "status": "missing_db",
        "db_path": str(tmp_path / "missing.db"),
        "min_trades": 25,
        "max_adjustment": 0.10,
        "strategies": [],
    }


def test_strategy_feedback_missing_tables(tmp_path: Path) -> None:
    db_path = tmp_path / "partial.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE paper_trades (id INTEGER PRIMARY KEY)")

    report = strategy_feedback_report(str(db_path))

    assert report["status"] == "missing_tables"
    assert report["missing_tables"] == ["paper_settlements"]
    assert report["strategies"] == []


def test_strategy_feedback_empty_data(tmp_path: Path) -> None:
    db_path = tmp_path / "empty.db"
    _init_db(db_path)

    report = strategy_feedback_report(str(db_path))

    assert report["status"] == "ok"
    assert report["strategies"] == []


def test_strategy_feedback_groups_metrics_and_caps_adjustments(tmp_path: Path) -> None:
    db_path = tmp_path / "feedback.db"
    _init_db(db_path)
    with sqlite3.connect(db_path) as conn:
        for pnl, roi in [(10.0, 0.30), (-4.0, -0.10), (8.0, 0.20)]:
            _trade(conn, " alpha ", pnl, roi)
        for label in (None, "", "   "):
            _trade(conn, label, -2.0, -0.20)
        _trade(conn, "beta", 5.0, 0.50)

    report = strategy_feedback_report(str(db_path), min_trades=3, max_adjustment=0.10)

    by_label = {row["strategy_label"]: row for row in report["strategies"]}
    assert by_label["alpha"] == {
        "strategy_label": "alpha",
        "closed_trades": 3,
        "win_rate": 2 / 3,
        "avg_roi": (0.30 - 0.10 + 0.20) / 3,
        "pnl": 14.0,
        "expectancy": 14.0 / 3,
        "eligible": True,
        "score_adjustment": 0.10,
        "reason": "eligible",
    }
    assert by_label["unknown"]["closed_trades"] == 3
    assert by_label["unknown"]["win_rate"] == 0.0
    assert by_label["unknown"]["avg_roi"] == pytest.approx(-0.20)
    assert by_label["unknown"]["pnl"] == -6.0
    assert by_label["unknown"]["expectancy"] == -2.0
    assert by_label["unknown"]["eligible"] is True
    assert by_label["unknown"]["score_adjustment"] == -0.10
    assert by_label["beta"]["eligible"] is False
    assert by_label["beta"]["score_adjustment"] == 0.0
    assert by_label["beta"]["reason"] == "needs at least 3 closed trades"


def test_strategy_feedback_cli_json(tmp_path: Path) -> None:
    db_path = tmp_path / "cli.db"
    _init_db(db_path)
    with sqlite3.connect(db_path) as conn:
        _trade(conn, "cli_strategy", 1.0, 0.05)

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "src.cli",
            "strategy-feedback",
            "--db-path",
            str(db_path),
            "--min-trades",
            "1",
            "--json",
        ],
        cwd=Path(__file__).resolve().parents[1],
        check=True,
        capture_output=True,
        text=True,
    )

    payload = json.loads(result.stdout)
    assert payload["status"] == "ok"
    assert payload["db_path"] == str(db_path)
    assert payload["min_trades"] == 1
    assert payload["max_adjustment"] == 0.10
    assert payload["strategies"][0]["strategy_label"] == "cli_strategy"
    assert payload["strategies"][0]["eligible"] is True
