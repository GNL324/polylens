from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import src.analysis.short_crypto_feedback_loop as loop
from src.analysis.short_crypto_feedback_loop import short_crypto_feedback_loop


def test_feedback_loop_reuses_existing_components(monkeypatch) -> None:
    calls: list[tuple[str, object]] = []

    def fake_settle_due(db_path: str) -> dict[str, object]:
        calls.append(("settle_due", db_path))
        return {"settled": 1}

    def fake_performance_report(db_path: str) -> dict[str, object]:
        calls.append(("performance_report", db_path))
        return {"closed_trades": 2}

    def fake_strategy_feedback_report(
        db_path: str,
        *,
        min_trades: int,
        max_adjustment: float,
    ) -> dict[str, object]:
        calls.append(("strategy_feedback_report", (db_path, min_trades, max_adjustment)))
        return {"status": "ok", "strategies": [{"strategy_label": "baseline"}]}

    def fake_strategy_recommendations_report(
        db_path: str,
        *,
        min_trades: int,
        max_adjustment: float,
    ) -> dict[str, object]:
        calls.append(("strategy_recommendations_report", (db_path, min_trades, max_adjustment)))
        return {"status": "ok", "recommendations": [{"strategy_label": "baseline", "recommendation": "hold"}]}

    monkeypatch.setattr(loop, "settle_due", fake_settle_due)
    monkeypatch.setattr(loop, "performance_report", fake_performance_report)
    monkeypatch.setattr(loop, "strategy_feedback_report", fake_strategy_feedback_report)
    monkeypatch.setattr(loop, "strategy_recommendations_report", fake_strategy_recommendations_report)

    result = short_crypto_feedback_loop("paper.db", min_trades=7, max_adjustment=0.05)

    assert calls == [
        ("settle_due", "paper.db"),
        ("performance_report", "paper.db"),
        ("strategy_feedback_report", ("paper.db", 7, 0.05)),
        ("strategy_recommendations_report", ("paper.db", 7, 0.05)),
    ]
    assert result == {
        "status": "ok",
        "db_path": "paper.db",
        "settlement": {"settled": 1},
        "paper_report": {"closed_trades": 2},
        "strategy_feedback": {"status": "ok", "strategies": [{"strategy_label": "baseline"}]},
        "strategy_recommendations": {"status": "ok", "recommendations": [{"strategy_label": "baseline", "recommendation": "hold"}]},
    }


def test_feedback_loop_propagates_feedback_status(monkeypatch) -> None:
    monkeypatch.setattr(loop, "settle_due", lambda db_path: {})
    monkeypatch.setattr(loop, "performance_report", lambda db_path: {})
    monkeypatch.setattr(
        loop,
        "strategy_feedback_report",
        lambda db_path, *, min_trades, max_adjustment: {"status": "missing_tables", "strategies": []},
    )
    monkeypatch.setattr(
        loop,
        "strategy_recommendations_report",
        lambda db_path, *, min_trades, max_adjustment: {"status": "missing_tables", "recommendations": []},
    )

    result = short_crypto_feedback_loop("paper.db")

    assert result["status"] == "missing_tables"
    assert result["strategy_feedback"]["status"] == "missing_tables"


def test_feedback_loop_cli_json(tmp_path: Path) -> None:
    db_path = tmp_path / "loop.db"

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "src.cli",
            "short-crypto-feedback-loop",
            "--db-path",
            str(db_path),
            "--min-trades",
            "1",
            "--max-adjustment",
            "0.05",
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
    assert "settlement" in payload
    assert payload["paper_report"]["total_trades"] == 0
    assert payload["strategy_feedback"]["min_trades"] == 1
    assert payload["strategy_feedback"]["max_adjustment"] == 0.05
    assert payload["strategy_feedback"]["strategies"] == []
    assert payload["strategy_recommendations"]["min_trades"] == 1
    assert payload["strategy_recommendations"]["max_adjustment"] == 0.05
    assert payload["strategy_recommendations"]["recommendations"] == []
