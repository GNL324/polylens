from __future__ import annotations

import csv
import json
import math
import sqlite3
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from statistics import mean, pstdev
from typing import Any

from src.analysis.kalshi_account_history_export import DEFAULT_ACCOUNT_HISTORY_PATH, load_kalshi_account_history
from src.analysis.kalshi_account_analytics import build_kalshi_account_report
from src.storage.kalshi_market_data import DEFAULT_KALSHI_DATA_DB


REGIMES = (
    "trending_up",
    "trending_down",
    "ranging",
    "high_volatility",
    "low_volatility",
    "breakout",
    "chop/noise",
)
STRATEGIES = (
    "momentum",
    "mean-reversion",
    "opposite-side",
    "no-trade-baseline",
    "probability-extremes",
)


def run_market_regime_analysis(
    db_path: str = DEFAULT_KALSHI_DATA_DB,
    *,
    orders_payload: Any | None = None,
    fills_payload: Any | None = None,
    account_history_path: str | Path = DEFAULT_ACCOUNT_HISTORY_PATH,
    edge_analysis_path: str | Path = "data/reports/kalshi_edge_analysis.json",
    fee_assumption: float = 0.0,
    export: bool = True,
) -> dict[str, Any]:
    series = _load_price_series(db_path)
    data_quality_warnings: list[str] = []
    if not series:
        data_quality_warnings.append(f"snapshot_db_missing_or_empty: {db_path}")
    account_history = load_kalshi_account_history(account_history_path)
    if orders_payload is None and fills_payload is None and account_history.get("available"):
        payload = account_history.get("payload") or {}
        orders_payload = payload.get("orders")
        fills_payload = payload.get("fills")
    elif orders_payload is None and fills_payload is None:
        data_quality_warnings.append(f"account_history_missing: {account_history.get('reason')}")
    regimes_by_ticker = {ticker: classify_market_regimes(rows) for ticker, rows in series.items()}
    regimes = _group_by_regime(series, regimes_by_ticker)
    regime_results = []
    for regime in REGIMES:
        regime_series = regimes.get(regime, {})
        strategies = [_evaluate_strategy(strategy, regime_series, fee_assumption=fee_assumption) for strategy in STRATEGIES]
        ranked = sorted(strategies, key=lambda row: (row["simulated_pnl"], row["win_rate"] or -1, -row["max_drawdown"]), reverse=True)
        for index, row in enumerate(ranked, start=1):
            row["rank"] = index
        regime_results.append(
            {
                "regime": regime,
                "ticker_count": len(regime_series),
                "tickers": sorted(regime_series),
                "strategies": ranked,
                "best_strategy": ranked[0]["strategy"] if ranked else None,
                "confidence_level": _confidence_level(sum(row["trade_count"] for row in strategies)),
            }
        )

    account_context = _account_context(orders_payload, fills_payload, series, regimes_by_ticker)
    edge_context = _load_edge_context(edge_analysis_path)
    if not edge_context.get("available"):
        data_quality_warnings.append(f"edge_analysis_missing: {edge_context.get('reason')}")
    explanation = _build_explanation(regime_results, account_context, edge_context)
    if explanation["old_trader_edge_classification"] == "insufficient_data":
        data_quality_warnings.append("insufficient_sample_size: account edge classification is insufficient_data")
    report = {
        "db_path": db_path,
        "summary": {
            "price_points": sum(len(rows) for rows in series.values()),
            "ticker_count": len(series),
            "best_regime_strategy": explanation["best_regime_strategy"],
            "old_trader_edge_classification": explanation["old_trader_edge_classification"],
            "had_directional_edge": explanation["had_directional_edge"],
            "had_inverted_edge": explanation["had_inverted_edge"],
            "had_timing_problem": explanation["had_timing_problem"],
            "had_no_detectable_edge": explanation["had_no_detectable_edge"],
            "why_prior_directional_trader_lost": explanation["why_prior_directional_trader_lost"],
            "conditions_that_would_have_worked": explanation["conditions_that_would_have_worked"],
            "data_quality_warnings": data_quality_warnings,
        },
        "regime_classification_by_ticker": regimes_by_ticker,
        "regimes": regime_results,
        "account_context": account_context,
        "edge_analysis_context": edge_context,
        "data_quality_warnings": data_quality_warnings,
    }
    if export:
        report["files"] = export_market_regime_analysis(report)
    return report


def classify_market_regimes(rows: list[dict[str, Any]]) -> list[str]:
    clean = [row for row in rows if row.get("mid_price") is not None]
    if len(clean) < 3:
        return ["low_volatility"]
    prices = [float(row["mid_price"]) for row in clean]
    deltas = [right - left for left, right in zip(prices, prices[1:])]
    total_move = prices[-1] - prices[0]
    price_range = max(prices) - min(prices)
    volatility = pstdev(deltas) if len(deltas) > 1 else 0.0
    avg_abs_move = mean(abs(delta) for delta in deltas) if deltas else 0.0
    direction_changes = sum(1 for left, right in zip(deltas, deltas[1:]) if left and right and (left > 0) != (right > 0))
    change_ratio = direction_changes / max(len(deltas) - 1, 1)
    labels: list[str] = []

    if change_ratio < 0.45 and total_move >= max(0.03, price_range * 0.45):
        labels.append("trending_up")
    elif change_ratio < 0.45 and total_move <= -max(0.03, price_range * 0.45):
        labels.append("trending_down")

    if price_range <= 0.08 and abs(total_move) <= 0.04:
        labels.append("ranging")
    if volatility >= 0.035 or avg_abs_move >= 0.035:
        labels.append("high_volatility")
    if volatility <= 0.012 and avg_abs_move <= 0.015:
        labels.append("low_volatility")
    if _has_breakout(prices):
        labels.append("breakout")
    if len(deltas) >= 4 and change_ratio >= 0.55 and price_range <= 0.18:
        labels.append("chop/noise")
    if not labels:
        labels.append("ranging")
    return labels


def export_market_regime_analysis(report: dict[str, Any], report_dir: str | Path = "data/reports") -> dict[str, str]:
    target = Path(report_dir)
    target.mkdir(parents=True, exist_ok=True)
    json_path = target / "market_regime_analysis.json"
    csv_path = target / "market_regime_analysis.csv"
    json_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        fields = [
            "regime",
            "rank",
            "strategy",
            "trade_count",
            "simulated_pnl",
            "win_rate",
            "average_return",
            "max_drawdown",
            "fees",
            "average_hold_time_seconds",
            "best_ticker",
            "worst_ticker",
            "confidence_level",
        ]
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for regime in report.get("regimes", []):
            for strategy in regime.get("strategies", []):
                writer.writerow({**{key: strategy.get(key) for key in fields}, "regime": regime.get("regime")})
    return {"json": str(json_path), "csv": str(csv_path)}


def _evaluate_strategy(strategy: str, series: dict[str, list[dict[str, Any]]], *, fee_assumption: float) -> dict[str, Any]:
    trades: list[dict[str, Any]] = []
    if strategy != "no-trade-baseline":
        for ticker, rows in series.items():
            clean = [row for row in rows if row.get("mid_price") is not None]
            if len(clean) < 2:
                continue
            if strategy == "momentum":
                trades.extend(_momentum_trades(ticker, clean, fee_assumption))
            elif strategy == "mean-reversion":
                trades.extend(_mean_reversion_trades(ticker, clean, fee_assumption))
            elif strategy == "opposite-side":
                trades.extend(_opposite_side_trades(ticker, clean, fee_assumption))
            elif strategy == "probability-extremes":
                trades.extend(_probability_extreme_trades(ticker, clean, fee_assumption))
    return _strategy_metrics(strategy, trades)


def _momentum_trades(ticker: str, rows: list[dict[str, Any]], fee: float) -> list[dict[str, Any]]:
    trades = []
    for prev, cur, nxt in zip(rows, rows[1:], rows[2:]):
        signal = float(cur["mid_price"]) - float(prev["mid_price"])
        if abs(signal) >= 0.01:
            direction = 1 if signal > 0 else -1
            pnl = direction * (float(nxt["mid_price"]) - float(cur["mid_price"]))
            trades.append(_trade("momentum", ticker, cur, nxt, pnl, fee, "followed prior price move"))
    return trades


def _mean_reversion_trades(ticker: str, rows: list[dict[str, Any]], fee: float) -> list[dict[str, Any]]:
    trades = []
    for index in range(1, len(rows) - 1):
        window = [float(row["mid_price"]) for row in rows[max(0, index - 3):index]]
        avg = mean(window)
        cur = rows[index]
        nxt = rows[index + 1]
        current = float(cur["mid_price"])
        if abs(current - avg) >= 0.015:
            direction = -1 if current > avg else 1
            pnl = direction * (float(nxt["mid_price"]) - current)
            trades.append(_trade("mean-reversion", ticker, cur, nxt, pnl, fee, "faded local price extension"))
    return trades


def _opposite_side_trades(ticker: str, rows: list[dict[str, Any]], fee: float) -> list[dict[str, Any]]:
    trades = []
    for prev, cur, nxt in zip(rows, rows[1:], rows[2:]):
        signal = float(cur["mid_price"]) - float(prev["mid_price"])
        if abs(signal) >= 0.01:
            direction = -1 if signal > 0 else 1
            pnl = direction * (float(nxt["mid_price"]) - float(cur["mid_price"]))
            trades.append(_trade("opposite-side", ticker, cur, nxt, pnl, fee, "inverted prior directional signal"))
    return trades


def _probability_extreme_trades(ticker: str, rows: list[dict[str, Any]], fee: float) -> list[dict[str, Any]]:
    trades = []
    for cur, nxt in zip(rows, rows[1:]):
        price = float(cur["mid_price"])
        if price <= 0.1:
            pnl = float(nxt["mid_price"]) - price
            trades.append(_trade("probability-extremes", ticker, cur, nxt, pnl, fee, "bought low-probability extreme"))
        elif price >= 0.9:
            pnl = price - float(nxt["mid_price"])
            trades.append(_trade("probability-extremes", ticker, cur, nxt, pnl, fee, "faded high-probability extreme"))
    return trades


def _trade(strategy: str, ticker: str, entry: dict[str, Any], exit_row: dict[str, Any], pnl: float, fee: float, reason: str) -> dict[str, Any]:
    net_pnl = float(pnl) - float(fee)
    return {
        "strategy": strategy,
        "ticker": ticker,
        "entry_time": entry.get("timestamp"),
        "exit_time": exit_row.get("timestamp"),
        "entry_price": round(float(entry["mid_price"]), 4),
        "exit_price": round(float(exit_row["mid_price"]), 4),
        "pnl": round(float(pnl), 4),
        "fee": round(float(fee), 4),
        "net_pnl": round(net_pnl, 4),
        "return": round(net_pnl / max(float(entry["mid_price"]), 0.01), 4),
        "hold_seconds": _hold_seconds(entry.get("timestamp"), exit_row.get("timestamp")),
        "reason": reason,
    }


def _strategy_metrics(strategy: str, trades: list[dict[str, Any]]) -> dict[str, Any]:
    net_values = [trade["net_pnl"] for trade in trades]
    pnl = round(sum(net_values), 4)
    fees = round(sum(trade["fee"] for trade in trades), 4)
    wins = sum(1 for value in net_values if value > 0)
    losses = sum(1 for value in net_values if value < 0)
    by_ticker: dict[str, float] = defaultdict(float)
    for trade in trades:
        by_ticker[trade["ticker"]] += trade["net_pnl"]
    best = max(by_ticker.items(), key=lambda item: item[1], default=(None, 0.0))
    worst = min(by_ticker.items(), key=lambda item: item[1], default=(None, 0.0))
    holds = [trade["hold_seconds"] for trade in trades if trade.get("hold_seconds") is not None]
    returns = [trade["return"] for trade in trades]
    return {
        "strategy": strategy,
        "trade_count": len(trades),
        "simulated_pnl": pnl,
        "win_rate": round(wins / (wins + losses), 4) if wins + losses else None,
        "average_return": round(mean(returns), 4) if returns else 0.0,
        "max_drawdown": _max_drawdown(net_values),
        "fees": fees,
        "average_hold_time_seconds": round(mean(holds), 2) if holds else None,
        "best_ticker": best[0],
        "best_ticker_pnl": round(best[1], 4),
        "worst_ticker": worst[0],
        "worst_ticker_pnl": round(worst[1], 4),
        "confidence_level": _confidence_level(len(trades)),
        "trades": trades,
    }


def _confidence_level(trade_count: int) -> str:
    if trade_count < 5:
        return "insufficient_data"
    if trade_count < 20:
        return "low"
    if trade_count < 50:
        return "medium"
    return "high"


def _load_price_series(db_path: str) -> dict[str, list[dict[str, Any]]]:
    path = Path(db_path)
    if not path.exists():
        return {}
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        try:
            rows = [dict(row) for row in conn.execute("SELECT * FROM kalshi_price_series ORDER BY ticker, timestamp, id").fetchall()]
        except sqlite3.OperationalError:
            return {}
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        if row.get("mid_price") is None:
            row["mid_price"] = _mid_price(row)
        grouped[row["ticker"]].append(row)
    return grouped


def _group_by_regime(series: dict[str, list[dict[str, Any]]], regimes_by_ticker: dict[str, list[str]]) -> dict[str, dict[str, list[dict[str, Any]]]]:
    grouped: dict[str, dict[str, list[dict[str, Any]]]] = {regime: {} for regime in REGIMES}
    for ticker, rows in series.items():
        for regime in regimes_by_ticker.get(ticker, []):
            if regime in grouped:
                grouped[regime][ticker] = rows
    return grouped


def _account_context(
    orders_payload: Any | None,
    fills_payload: Any | None,
    series: dict[str, list[dict[str, Any]]],
    regimes_by_ticker: dict[str, list[str]],
) -> dict[str, Any]:
    if orders_payload is None and fills_payload is None:
        return {"available": False, "reason": "authenticated Kalshi fills/orders were not available"}
    report = build_kalshi_account_report(None, None, orders_payload, fills_payload)
    trades = report.get("fills") or report.get("orders") or []
    by_regime: dict[str, dict[str, Any]] = defaultdict(lambda: {"trade_count": 0, "tickers": set()})
    for trade in trades:
        ticker = str(trade.get("ticker") or trade.get("market_ticker") or "")
        if ticker not in series:
            continue
        for regime in regimes_by_ticker.get(ticker, []):
            by_regime[regime]["trade_count"] += 1
            by_regime[regime]["tickers"].add(ticker)
    return {
        "available": True,
        "trade_count": len(trades),
        "by_regime": {key: {"trade_count": value["trade_count"], "tickers": sorted(value["tickers"])} for key, value in by_regime.items()},
    }


def _load_edge_context(path: str | Path) -> dict[str, Any]:
    edge_path = Path(path)
    if not edge_path.exists():
        return {"available": False, "reason": f"{edge_path} not found"}
    try:
        payload = json.loads(edge_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {"available": False, "reason": f"could not read {edge_path}: {exc}"}
    summary = payload.get("summary") if isinstance(payload, dict) else {}
    return {
        "available": True,
        "path": str(edge_path),
        "edge_classification": summary.get("edge_classification") or summary.get("old_trader_edge_classification"),
        "has_directional_edge": bool(summary.get("has_directional_edge")),
        "has_inverted_edge": bool(summary.get("has_inverted_edge")),
        "has_timing_problem": bool(summary.get("has_timing_problem")),
        "has_no_detectable_edge": bool(summary.get("has_no_detectable_edge")),
        "best_variant": summary.get("best_variant"),
    }


def _build_explanation(regime_results: list[dict[str, Any]], account_context: dict[str, Any], edge_context: dict[str, Any]) -> dict[str, Any]:
    profitable = [
        (regime["regime"], strategy)
        for regime in regime_results
        for strategy in regime.get("strategies", [])
        if strategy["strategy"] != "no-trade-baseline" and strategy["simulated_pnl"] > 0 and strategy["confidence_level"] != "insufficient_data"
    ]
    best = max(profitable, key=lambda item: item[1]["simulated_pnl"], default=None)
    directionals = [
        (regime["regime"], strategy)
        for regime in regime_results
        for strategy in regime.get("strategies", [])
        if strategy["strategy"] in {"momentum", "probability-extremes"} and strategy["trade_count"] >= 5
    ]
    losing_directionals = [item for item in directionals if item[1]["simulated_pnl"] < 0]
    edge_label = edge_context.get("edge_classification") if edge_context.get("available") else None
    if not edge_label:
        edge_label = "insufficient_data" if not account_context.get("available") else "no_detectable_edge"
    why = "Insufficient account edge data; regime results show whether directional strategies were structurally weak in recorded snapshots."
    if losing_directionals:
        worst_regime, worst_strategy = min(losing_directionals, key=lambda item: item[1]["simulated_pnl"])
        why = f"Directional crypto entries lost most in {worst_regime}, where {worst_strategy['strategy']} produced simulated_pnl={worst_strategy['simulated_pnl']}."
    conditions = "No positive strategy/regime combination had enough samples to support a condition."
    if best:
        conditions = f"It would have worked best in {best[0]} using {best[1]['strategy']} with simulated_pnl={best[1]['simulated_pnl']} and confidence={best[1]['confidence_level']}."
    return {
        "best_regime_strategy": {"regime": best[0], "strategy": best[1]["strategy"], "simulated_pnl": best[1]["simulated_pnl"]} if best else None,
        "old_trader_edge_classification": edge_label,
        "had_directional_edge": bool(edge_context.get("has_directional_edge")),
        "had_inverted_edge": bool(edge_context.get("has_inverted_edge")),
        "had_timing_problem": bool(edge_context.get("has_timing_problem")),
        "had_no_detectable_edge": bool(edge_context.get("has_no_detectable_edge")) or edge_label in {"no_detectable_edge", "insufficient_data"},
        "why_prior_directional_trader_lost": why,
        "conditions_that_would_have_worked": conditions,
    }


def _has_breakout(prices: list[float]) -> bool:
    if len(prices) < 6:
        return False
    for index in range(4, len(prices)):
        prior = prices[index - 4:index]
        prior_range = max(prior) - min(prior)
        if prices[index] > max(prior) + max(0.025, prior_range * 0.5):
            return True
        if prices[index] < min(prior) - max(0.025, prior_range * 0.5):
            return True
    return False


def _max_drawdown(values: list[float]) -> float:
    total = 0.0
    peak = 0.0
    max_dd = 0.0
    for value in values:
        total += value
        peak = max(peak, total)
        max_dd = min(max_dd, total - peak)
    return round(abs(max_dd), 4)


def _hold_seconds(left: Any, right: Any) -> float | None:
    try:
        return max(0.0, (_parse_time(right) - _parse_time(left)).total_seconds())
    except (TypeError, ValueError):
        return None


def _parse_time(value: Any) -> datetime:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def _mid_price(row: dict[str, Any]) -> float | None:
    bid = row.get("yes_bid")
    ask = row.get("yes_ask")
    if bid is None or ask is None:
        return None
    return round((float(bid) + float(ask)) / 2, 4)
