from __future__ import annotations

import json

from src.analysis.kalshi_strategy_lab import run_kalshi_strategy_lab


def _write_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _edge_report(opposite_pnl: float = 12.0, actual_pnl: float = -10.0, sample: int = 30):
    return {
        "summary": {
            "classification": "inverted_edge" if opposite_pnl > 0 and actual_pnl < 0 else "no_detectable_edge",
            "has_inverted_edge": opposite_pnl > 0 and actual_pnl < 0,
            "sample_size": sample,
        },
        "variant_rankings": [
            {"variant": "opposite_side_trades", "trade_count": sample, "pnl": opposite_pnl, "win_rate": 0.7, "average_return": 0.12, "max_drawdown": 1.0},
            {"variant": "actual_trades", "trade_count": sample, "pnl": actual_pnl, "win_rate": 0.3, "average_return": -0.1, "max_drawdown": 12.0},
        ],
    }


def _strategy(strategy: str, trade_count: int, pnl: float, win_rate: float = 0.7, avg_return: float = 0.1, drawdown: float = 1.0):
    return {
        "strategy": strategy,
        "trade_count": trade_count,
        "simulated_pnl": pnl,
        "win_rate": win_rate,
        "average_return": avg_return,
        "max_drawdown": drawdown,
        "average_hold_time_seconds": 60,
    }


def _regime_report(momentum_pnl: float = 15.0, mean_reversion_pnl: float = 8.0, choppy_loss: float = -9.0, sample: int = 30):
    return {
        "regimes": [
            {"regime": "trending_up", "strategies": [_strategy("momentum", sample, momentum_pnl), _strategy("mean-reversion", sample, -4.0, 0.3, -0.05, 4.0)]},
            {"regime": "ranging", "strategies": [_strategy("mean-reversion", sample, mean_reversion_pnl), _strategy("momentum", sample, -3.0, 0.35, -0.03, 3.0)]},
            {"regime": "chop/noise", "strategies": [_strategy("momentum", sample, choppy_loss, 0.25, -0.08, 9.0), _strategy("mean-reversion", sample, -2.0, 0.4, -0.02, 2.0)]},
            {"regime": "high_volatility", "strategies": [_strategy("mean-reversion", sample, mean_reversion_pnl / 2), _strategy("momentum", sample, choppy_loss / 2, 0.3, -0.04, 5.0)]},
        ]
    }


def _write_inputs(tmp_path, edge=None, regime=None):
    account = tmp_path / "data" / "raw" / "kalshi_account_history.json"
    snapshot = tmp_path / "data" / "kalshi_market_data.db"
    edge_path = tmp_path / "data" / "reports" / "kalshi_edge_analysis.json"
    regime_path = tmp_path / "data" / "reports" / "market_regime_analysis.json"
    _write_json(account, {"fills": {"fills": []}})
    snapshot.parent.mkdir(parents=True, exist_ok=True)
    snapshot.write_bytes(b"")
    _write_json(edge_path, edge or _edge_report())
    _write_json(regime_path, regime or _regime_report())
    return account, snapshot, edge_path, regime_path


def _run(tmp_path, **kwargs):
    account, snapshot, edge, regime = _write_inputs(tmp_path, kwargs.pop("edge", None), kwargs.pop("regime", None))
    return run_kalshi_strategy_lab(
        account_history_path=account,
        snapshot_path=snapshot,
        edge_analysis_path=edge,
        regime_analysis_path=regime,
        min_sample_size=kwargs.pop("min_sample_size", 20),
        min_confidence=kwargs.pop("min_confidence", 0.6),
        export=kwargs.pop("export", False),
        **kwargs,
    )


def test_insufficient_data_rejects_all_strategies(tmp_path):
    result = _run(tmp_path, edge=_edge_report(sample=2), regime=_regime_report(sample=2), min_sample_size=20)

    assert all(row["rejected"] for row in result["ranked_strategies"])
    assert set(result["summary"]["strategies_rejected_due_to_insufficient_data"]) >= {"opposite-trader", "regime-filtered-momentum"}


def test_opposite_trader_ranks_high_when_historical_behavior_is_inverted(tmp_path):
    result = _run(tmp_path, edge=_edge_report(opposite_pnl=25, actual_pnl=-20), regime=_regime_report(momentum_pnl=5, mean_reversion_pnl=4))

    assert result["ranked_strategies"][0]["strategy"] == "opposite-trader"
    assert result["ranked_strategies"][0]["rejected"] is False


def test_momentum_ranks_high_in_trending_mock_data(tmp_path):
    result = _run(tmp_path, edge=_edge_report(opposite_pnl=2, actual_pnl=-1), regime=_regime_report(momentum_pnl=30, mean_reversion_pnl=3))

    assert result["ranked_strategies"][0]["strategy"] == "regime-filtered-momentum"


def test_mean_reversion_ranks_high_in_ranging_mock_data(tmp_path):
    result = _run(tmp_path, edge=_edge_report(opposite_pnl=1, actual_pnl=0), regime=_regime_report(momentum_pnl=2, mean_reversion_pnl=30))

    assert result["ranked_strategies"][0]["strategy"] == "regime-filtered-mean-reversion"


def test_no_trade_filter_blocks_noisy_choppy_data(tmp_path):
    result = _run(tmp_path, edge=_edge_report(opposite_pnl=1, actual_pnl=0), regime=_regime_report(momentum_pnl=1, mean_reversion_pnl=1, choppy_loss=-35))
    no_trade = next(row for row in result["ranked_strategies"] if row["strategy"] == "no-trade-filter")

    assert no_trade["rejected"] is False
    assert no_trade["simulated_pnl"] > 0
    assert "Blocks trading" in no_trade["reason"]


def test_json_export_works(tmp_path, monkeypatch):
    account, snapshot, edge, regime = _write_inputs(tmp_path)
    monkeypatch.chdir(tmp_path)

    result = run_kalshi_strategy_lab(account_history_path=account, snapshot_path=snapshot, edge_analysis_path=edge, regime_analysis_path=regime, export=True)

    assert result["files"]["json"] == "data/reports/kalshi_strategy_lab.json"
    assert result["files"]["csv"] == "data/reports/kalshi_strategy_lab.csv"
    assert (tmp_path / "data" / "reports" / "kalshi_strategy_lab.json").exists()
    assert (tmp_path / "data" / "reports" / "kalshi_strategy_lab.csv").read_text(encoding="utf-8").startswith("rank,strategy")
