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

from src.sqlite_utils import closing_connection
from src.storage.kalshi_market_data import DEFAULT_KALSHI_DATA_DB

STRATEGIES = {"spread-compression", "momentum", "mean-reversion", "probability-extremes"}


def run_kalshi_backtest(db_path: str = DEFAULT_KALSHI_DATA_DB, *, strategy: str = "all", fee_assumption: float = 0.0, spread_threshold: float = 0.05, bankroll: float = 1000.0, export: bool = False) -> dict[str, Any]:
    strategies = sorted(STRATEGIES) if strategy == "all" else [strategy]
    unknown = [item for item in strategies if item not in STRATEGIES]
    if unknown:
        raise ValueError(f"unknown strategy: {unknown[0]}")
    series = _load_price_series(db_path)
    market_rows = _load_market_snapshots(db_path)
    results = [_run_strategy(name, series, fee_assumption=fee_assumption, spread_threshold=spread_threshold, bankroll=bankroll) for name in strategies]
    summary = summarize_backtest_results({"strategies": results, "data": _data_summary(series, market_rows)})
    report = {"db_path": db_path, "strategy_requested": strategy, "strategies": results, "data": _data_summary(series, market_rows), "summary": summary}
    if export:
        report["exported_files"] = export_kalshi_backtest(report)
    return report


def summarize_kalshi_backtests(db_path: str = DEFAULT_KALSHI_DATA_DB) -> dict[str, Any]:
    report = run_kalshi_backtest(db_path=db_path, strategy="all", export=False)
    return report["summary"] | {"data": report["data"]}


def summarize_backtest_results(report: dict[str, Any]) -> dict[str, Any]:
    strategies = report.get("strategies", [])
    if not strategies:
        return {}
    ranked = sorted(strategies, key=lambda row: row.get("net_pnl", 0), reverse=True)
    asset_scores: dict[str, float] = defaultdict(float)
    for strategy in strategies:
        for trade in strategy.get("trades", []):
            asset_scores[trade.get("asset") or "unknown"] += trade.get("pnl", 0) - trade.get("fee", 0)
    assets = sorted(asset_scores.items(), key=lambda item: item[1], reverse=True)
    market_rows = (report.get("data") or {}).get("markets", [])
    high_spread = sorted(market_rows, key=lambda row: row.get("max_spread") or 0, reverse=True)[:10]
    volatile = sorted(market_rows, key=lambda row: row.get("volatility") or 0, reverse=True)[:10]
    return {
        "best_strategy": _brief_strategy(ranked[0]),
        "worst_strategy": _brief_strategy(ranked[-1]),
        "best_asset": {"asset": assets[0][0], "net_pnl": round(assets[0][1], 4)} if assets else None,
        "worst_asset": {"asset": assets[-1][0], "net_pnl": round(assets[-1][1], 4)} if assets else None,
        "highest_spread_markets": high_spread,
        "most_volatile_markets": volatile,
    }


def export_kalshi_backtest(report: dict[str, Any], report_dir: str | Path = "data/reports") -> dict[str, str]:
    target = Path(report_dir)
    target.mkdir(parents=True, exist_ok=True)
    json_path = target / "kalshi_backtest.json"
    csv_path = target / "kalshi_backtest.csv"
    json_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        fields = ["strategy", "ticker", "asset", "entry_time", "exit_time", "entry_price", "exit_price", "pnl", "fee", "net_pnl", "hold_seconds", "spread_captured", "reason"]
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for strategy in report.get("strategies", []):
            for trade in strategy.get("trades", []):
                writer.writerow({key: trade.get(key) for key in fields})
    return {"json": str(json_path), "csv": str(csv_path)}


def _run_strategy(name: str, series: dict[str, list[dict[str, Any]]], *, fee_assumption: float, spread_threshold: float, bankroll: float) -> dict[str, Any]:
    trades: list[dict[str, Any]] = []
    for ticker, rows in series.items():
        if len(rows) < 2:
            continue
        if name == "spread-compression":
            trades.extend(_spread_compression_trades(ticker, rows, spread_threshold, fee_assumption))
        elif name == "momentum":
            trades.extend(_momentum_trades(ticker, rows, fee_assumption))
        elif name == "mean-reversion":
            trades.extend(_mean_reversion_trades(ticker, rows, fee_assumption))
        elif name == "probability-extremes":
            trades.extend(_probability_extreme_trades(ticker, rows, fee_assumption))
    return _strategy_metrics(name, trades, bankroll)


def _spread_compression_trades(ticker: str, rows: list[dict[str, Any]], threshold: float, fee: float) -> list[dict[str, Any]]:
    trades = []
    open_row = None
    for row in rows:
        spread = row.get("spread")
        if spread is None:
            continue
        if open_row is None and spread >= threshold:
            open_row = row
        elif open_row and spread <= threshold / 2:
            entry = open_row.get("mid_price") or open_row.get("yes_ask") or 0
            exit_price = row.get("mid_price") or row.get("yes_bid") or entry
            pnl = abs(open_row["spread"] - spread)
            trades.append(_trade("spread-compression", ticker, open_row, row, entry, exit_price, pnl, fee, open_row["spread"] - spread, "spread normalized"))
            open_row = None
    return trades


def _momentum_trades(ticker: str, rows: list[dict[str, Any]], fee: float) -> list[dict[str, Any]]:
    trades = []
    for prev, cur in zip(rows, rows[1:]):
        p0, p1 = prev.get("mid_price"), cur.get("mid_price")
        if p0 is None or p1 is None:
            continue
        move = p1 - p0
        if abs(move) >= 0.01:
            trades.append(_trade("momentum", ticker, prev, cur, p0, p1, move, fee, cur.get("spread"), "followed short-term price move"))
    return trades


def _mean_reversion_trades(ticker: str, rows: list[dict[str, Any]], fee: float) -> list[dict[str, Any]]:
    trades = []
    mids = [row.get("mid_price") for row in rows if row.get("mid_price") is not None]
    if len(mids) < 2:
        return trades
    avg = mean(mids)
    for prev, cur in zip(rows, rows[1:]):
        p0, p1 = prev.get("mid_price"), cur.get("mid_price")
        if p0 is None or p1 is None:
            continue
        if abs(p0 - avg) >= 0.05:
            pnl = (p0 - p1) if p0 > avg else (p1 - p0)
            trades.append(_trade("mean-reversion", ticker, prev, cur, p0, p1, pnl, fee, cur.get("spread"), "faded extreme move"))
    return trades


def _probability_extreme_trades(ticker: str, rows: list[dict[str, Any]], fee: float) -> list[dict[str, Any]]:
    trades = []
    for prev, cur in zip(rows, rows[1:]):
        p0, p1 = prev.get("mid_price"), cur.get("mid_price")
        if p0 is None or p1 is None:
            continue
        if 0.01 <= p0 <= 0.05:
            trades.append(_trade("probability-extremes", ticker, prev, cur, p0, p1, p1 - p0, fee, cur.get("spread"), "low-probability band"))
        elif 0.95 <= p0 <= 0.99:
            trades.append(_trade("probability-extremes", ticker, prev, cur, p0, p1, p0 - p1, fee, cur.get("spread"), "high-probability band"))
    return trades


def _trade(strategy: str, ticker: str, entry: dict[str, Any], exit_row: dict[str, Any], entry_price: float, exit_price: float, pnl: float, fee: float, spread_captured: float | None, reason: str) -> dict[str, Any]:
    hold = max(0.0, (_parse_time(exit_row["timestamp"]) - _parse_time(entry["timestamp"])).total_seconds())
    return {
        "strategy": strategy,
        "ticker": ticker,
        "asset": entry.get("asset") or exit_row.get("asset"),
        "market_type": entry.get("market_type") or exit_row.get("market_type"),
        "entry_time": entry.get("timestamp"),
        "exit_time": exit_row.get("timestamp"),
        "entry_price": round(float(entry_price), 4),
        "exit_price": round(float(exit_price), 4),
        "pnl": round(float(pnl), 4),
        "fee": round(float(fee), 4),
        "net_pnl": round(float(pnl) - float(fee), 4),
        "hold_seconds": hold,
        "spread_captured": round(float(spread_captured), 4) if spread_captured is not None else None,
        "reason": reason,
    }


def _strategy_metrics(name: str, trades: list[dict[str, Any]], bankroll: float) -> dict[str, Any]:
    net_values = [trade["net_pnl"] for trade in trades]
    pnl = round(sum(trade["pnl"] for trade in trades), 4)
    fees = round(sum(trade["fee"] for trade in trades), 4)
    wins = sum(1 for value in net_values if value > 0)
    losses = sum(1 for value in net_values if value < 0)
    curve = []
    total = 0.0
    for value in net_values:
        total += value
        curve.append(total)
    score = round((mean(net_values) / pstdev(net_values)) * math.sqrt(len(net_values)), 4) if len(net_values) > 1 and pstdev(net_values) else None
    spreads = [trade["spread_captured"] for trade in trades if trade.get("spread_captured") is not None]
    holds = [trade["hold_seconds"] for trade in trades]
    return {
        "strategy": name,
        "trade_count": len(trades),
        "pnl": pnl,
        "fees": fees,
        "net_pnl": round(pnl - fees, 4),
        "roi": round((pnl - fees) / bankroll, 4) if bankroll else None,
        "sharpe_like_score": score,
        "max_drawdown": _max_drawdown(curve),
        "win_rate": round(wins / (wins + losses), 4) if wins + losses else None,
        "win_count": wins,
        "loss_count": losses,
        "average_hold_time_seconds": round(mean(holds), 2) if holds else None,
        "average_spread_captured": round(mean(spreads), 4) if spreads else None,
        "trades": trades,
    }


def _load_price_series(db_path: str) -> dict[str, list[dict[str, Any]]]:
    path = Path(db_path)
    if not path.exists():
        return {}
    with closing_connection(path, row_factory=sqlite3.Row) as conn:
        rows = [dict(row) for row in conn.execute("SELECT * FROM kalshi_price_series ORDER BY ticker, timestamp, id").fetchall()]
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[row["ticker"]].append(row)
    return grouped


def _load_market_snapshots(db_path: str) -> list[dict[str, Any]]:
    path = Path(db_path)
    if not path.exists():
        return []
    with closing_connection(path, row_factory=sqlite3.Row) as conn:
        try:
            rows = [dict(row) for row in conn.execute("SELECT * FROM kalshi_market_snapshots").fetchall()]
        except sqlite3.OperationalError:
            rows = []
    return rows


def _data_summary(series: dict[str, list[dict[str, Any]]], market_rows: list[dict[str, Any]]) -> dict[str, Any]:
    market_summary = []
    for ticker, rows in series.items():
        mids = [row.get("mid_price") for row in rows if row.get("mid_price") is not None]
        spreads = [row.get("spread") for row in rows if row.get("spread") is not None]
        market_summary.append({
            "ticker": ticker,
            "asset": next((row.get("asset") for row in rows if row.get("asset")), None),
            "points": len(rows),
            "max_spread": max(spreads) if spreads else None,
            "average_spread": round(mean(spreads), 4) if spreads else None,
            "volatility": round(pstdev(mids), 4) if len(mids) > 1 else 0,
        })
    return {
        "price_points": sum(len(rows) for rows in series.values()),
        "tickers": len(series),
        "market_snapshots": len(market_rows),
        "assets": sorted({row.get("asset") for rows in series.values() for row in rows if row.get("asset")}),
        "markets": market_summary,
    }


def _brief_strategy(row: dict[str, Any]) -> dict[str, Any]:
    return {key: row.get(key) for key in ("strategy", "trade_count", "net_pnl", "roi", "win_rate", "max_drawdown", "sharpe_like_score")}


def _max_drawdown(curve: list[float]) -> float:
    peak = 0.0
    max_dd = 0.0
    for value in curve:
        peak = max(peak, value)
        max_dd = min(max_dd, value - peak)
    return round(abs(max_dd), 4)


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
