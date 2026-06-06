from __future__ import annotations

import csv
import json
import sqlite3
from pathlib import Path
from statistics import mean
from typing import Any

from src.analysis.kalshi_account_history_export import DEFAULT_ACCOUNT_HISTORY_PATH, load_kalshi_account_history
from src.analysis.kalshi_edge_analysis import DEFAULT_EDGE_REPORT_JSON
from src.storage.kalshi_market_data import DEFAULT_KALSHI_DATA_DB


DEFAULT_REGIME_REPORT_JSON = "data/reports/market_regime_analysis.json"
DEFAULT_STRATEGY_LAB_JSON = "data/reports/kalshi_strategy_lab.json"
DEFAULT_STRATEGY_LAB_CSV = "data/reports/kalshi_strategy_lab.csv"
STRATEGIES = (
    "opposite-trader",
    "regime-filtered-momentum",
    "regime-filtered-mean-reversion",
    "no-trade-filter",
    "confidence-threshold",
)


def run_kalshi_strategy_lab(
    *,
    account_history_path: str | Path = DEFAULT_ACCOUNT_HISTORY_PATH,
    snapshot_path: str | Path = DEFAULT_KALSHI_DATA_DB,
    edge_analysis_path: str | Path = DEFAULT_EDGE_REPORT_JSON,
    regime_analysis_path: str | Path = DEFAULT_REGIME_REPORT_JSON,
    min_sample_size: int = 20,
    min_confidence: float = 0.6,
    bankroll: float = 1000.0,
    max_risk_per_trade: float = 0.02,
    export: bool = False,
) -> dict[str, Any]:
    account = load_kalshi_account_history(account_history_path)
    edge = _load_json(edge_analysis_path)
    regime = _load_json(regime_analysis_path)
    snapshot_summary = _snapshot_summary(snapshot_path)
    warnings = _input_warnings(account, edge, regime, snapshot_summary, account_history_path, edge_analysis_path, regime_analysis_path, snapshot_path)

    candidates = [
        _opposite_trader(edge, min_sample_size, min_confidence),
        _regime_strategy("regime-filtered-momentum", regime, {"trending_up", "trending_down", "breakout"}, "momentum", min_sample_size, min_confidence),
        _regime_strategy("regime-filtered-mean-reversion", regime, {"ranging", "high_volatility"}, "mean-reversion", min_sample_size, min_confidence),
        _no_trade_filter(regime, min_sample_size, min_confidence),
    ]
    candidates.append(_confidence_threshold(candidates[:3], min_sample_size, min_confidence, bankroll, max_risk_per_trade))
    ranked = sorted(candidates, key=lambda row: (not row["rejected"], row["simulated_pnl"], row["confidence_score"], row["win_rate"] or 0), reverse=True)
    for index, row in enumerate(ranked, start=1):
        row["rank"] = index

    accepted = [row for row in ranked if not row["rejected"]]
    rejected = [row for row in ranked if row["rejected"]]
    best = accepted[0] if accepted else None
    safe_to_paper = bool(best and best["confidence_score"] >= min_confidence and best["trade_count"] >= min_sample_size and best["simulated_pnl"] > 0)
    report = {
        "summary": {
            "best_strategy": best["strategy"] if best else None,
            "best_candidate_strategy": best["strategy"] if best else None,
            "safe_enough_to_paper_trade": safe_to_paper,
            "strategies_rejected_due_to_insufficient_data": [row["strategy"] for row in rejected if row["rejection_reason"] == "insufficient_data"],
            "recommended_next_data_to_collect": _recommended_next_data(warnings, ranked, min_sample_size),
            "data_quality_warnings": warnings,
            "bankroll": bankroll,
            "max_risk_per_trade": max_risk_per_trade,
            "min_sample_size": min_sample_size,
            "min_confidence": min_confidence,
        },
        "inputs": {
            "account_history_path": str(account_history_path),
            "snapshot_path": str(snapshot_path),
            "edge_analysis_path": str(edge_analysis_path),
            "regime_analysis_path": str(regime_analysis_path),
            "snapshot_summary": snapshot_summary,
        },
        "ranked_strategies": ranked,
        "rejected_strategies": rejected,
    }
    if export:
        report["files"] = export_kalshi_strategy_lab(report)
    return report


def export_kalshi_strategy_lab(report: dict[str, Any], report_dir: str | Path = "data/reports") -> dict[str, str]:
    target = Path(report_dir)
    target.mkdir(parents=True, exist_ok=True)
    json_path = target / "kalshi_strategy_lab.json"
    csv_path = target / "kalshi_strategy_lab.csv"
    json_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    fields = [
        "rank",
        "strategy",
        "trade_count",
        "simulated_pnl",
        "win_rate",
        "max_drawdown",
        "average_return",
        "average_hold_time",
        "required_minimum_sample_size",
        "confidence_score",
        "rejected",
        "rejection_reason",
        "reason",
    ]
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in report.get("ranked_strategies", []):
            writer.writerow({key: row.get(key) for key in fields})
    return {"json": str(json_path), "csv": str(csv_path)}


def _opposite_trader(edge: dict[str, Any], min_sample_size: int, min_confidence: float) -> dict[str, Any]:
    variant = _variant(edge, "opposite_side_trades")
    actual = _variant(edge, "actual_trades")
    sample = int(variant.get("trade_count") or 0)
    pnl = float(variant.get("pnl") or 0)
    confidence = _confidence(sample, variant.get("win_rate"), variant.get("average_return"), min_sample_size)
    inverted = bool((edge.get("summary") or {}).get("has_inverted_edge")) or pnl > max(float(actual.get("pnl") or 0), 0)
    rejected, reason_code = _reject(sample, confidence, pnl, min_sample_size, min_confidence)
    reason = "Takes the opposite side of historically losing account behavior." if inverted else "No strong inverted edge was found in edge analysis."
    if not inverted and not rejected:
        rejected, reason_code = True, "weak_edge"
    return _candidate("opposite-trader", variant, min_sample_size, confidence, rejected, reason_code, reason)


def _regime_strategy(
    name: str,
    regime_report: dict[str, Any],
    regimes: set[str],
    strategy_name: str,
    min_sample_size: int,
    min_confidence: float,
) -> dict[str, Any]:
    rows = [
        strategy
        for regime in regime_report.get("regimes", [])
        if regime.get("regime") in regimes
        for strategy in regime.get("strategies", [])
        if strategy.get("strategy") == strategy_name
    ]
    metrics = _combine_strategy_rows(rows)
    confidence = _confidence(metrics["trade_count"], metrics["win_rate"], metrics["average_return"], min_sample_size)
    rejected, reason_code = _reject(metrics["trade_count"], confidence, metrics["simulated_pnl"], min_sample_size, min_confidence)
    if name.endswith("momentum"):
        reason = "Trades directional momentum only when recorded regimes are trending or breaking out."
    else:
        reason = "Trades mean reversion only when recorded regimes are ranging or high-volatility."
    return _candidate(name, metrics, min_sample_size, confidence, rejected, reason_code, reason)


def _no_trade_filter(regime_report: dict[str, Any], min_sample_size: int, min_confidence: float) -> dict[str, Any]:
    noisy = [
        strategy
        for regime in regime_report.get("regimes", [])
        if regime.get("regime") in {"chop/noise", "high_volatility"}
        for strategy in regime.get("strategies", [])
        if strategy.get("strategy") != "no-trade-baseline"
    ]
    trade_count = sum(int(row.get("trade_count") or 0) for row in noisy)
    worst_loss = min((float(row.get("simulated_pnl") or 0) for row in noisy), default=0.0)
    avoided_loss = abs(worst_loss) if worst_loss < 0 else 0.0
    confidence = min(1.0, (trade_count / max(min_sample_size, 1)) * (1.0 if avoided_loss > 0 else 0.4))
    rejected = trade_count < min_sample_size or confidence < min_confidence or avoided_loss <= 0
    reason_code = "insufficient_data" if trade_count < min_sample_size else "low_confidence" if confidence < min_confidence else "weak_edge" if avoided_loss <= 0 else None
    metrics = {
        "trade_count": trade_count,
        "simulated_pnl": round(avoided_loss, 4),
        "win_rate": 1.0 if avoided_loss > 0 else None,
        "max_drawdown": 0.0,
        "average_return": round(avoided_loss / max(trade_count, 1), 4),
        "average_hold_time": _avg([row.get("average_hold_time_seconds") for row in noisy]),
    }
    reason = "Blocks trading in noisy/choppy regimes where active strategies historically lost money."
    return _candidate("no-trade-filter", metrics, min_sample_size, confidence, rejected, reason_code, reason)


def _confidence_threshold(candidates: list[dict[str, Any]], min_sample_size: int, min_confidence: float, bankroll: float, max_risk_per_trade: float) -> dict[str, Any]:
    eligible = [row for row in candidates if not row["rejected"] and row["confidence_score"] >= min_confidence and row["trade_count"] >= min_sample_size]
    best = max(eligible, key=lambda row: row["simulated_pnl"], default=None)
    trade_count = int(best["trade_count"]) if best else 0
    pnl = float(best["simulated_pnl"]) * 0.95 if best else 0.0
    confidence = _avg([row["confidence_score"] for row in eligible]) or 0.0
    rejected = not eligible
    reason_code = "insufficient_data" if not eligible else None
    risk_budget = bankroll * max_risk_per_trade
    metrics = {
        "trade_count": trade_count,
        "simulated_pnl": round(pnl, 4),
        "win_rate": best.get("win_rate") if best else None,
        "max_drawdown": round(float(best.get("max_drawdown") or 0), 4) if best else 0.0,
        "average_return": float(best.get("average_return") or 0) if best else 0.0,
        "average_hold_time": best.get("average_hold_time") if best else None,
    }
    source = best["strategy"] if best else "none"
    reason = f"Allows only candidates meeting sample/confidence thresholds; strongest qualifying source is {source}; max paper risk per trade is {risk_budget:.2f}."
    return _candidate("confidence-threshold", metrics, min_sample_size, confidence, rejected, reason_code, reason)


def _candidate(
    name: str,
    metrics: dict[str, Any],
    min_sample_size: int,
    confidence: float,
    rejected: bool,
    rejection_reason: str | None,
    reason: str,
) -> dict[str, Any]:
    return {
        "strategy": name,
        "trade_count": int(metrics.get("trade_count") or 0),
        "simulated_pnl": round(float(metrics.get("simulated_pnl", metrics.get("pnl", 0)) or 0), 4),
        "win_rate": metrics.get("win_rate"),
        "max_drawdown": round(float(metrics.get("max_drawdown") or 0), 4),
        "average_return": round(float(metrics.get("average_return") or 0), 4),
        "average_hold_time": metrics.get("average_hold_time") if metrics.get("average_hold_time") is not None else metrics.get("average_hold_time_seconds"),
        "required_minimum_sample_size": min_sample_size,
        "confidence_score": round(confidence, 4),
        "rejected": bool(rejected),
        "rejection_reason": rejection_reason,
        "reason": reason,
    }


def _reject(sample: int, confidence: float, pnl: float, min_sample_size: int, min_confidence: float) -> tuple[bool, str | None]:
    if sample < min_sample_size:
        return True, "insufficient_data"
    if confidence < min_confidence:
        return True, "low_confidence"
    if pnl <= 0:
        return True, "negative_edge"
    return False, None


def _combine_strategy_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    trade_count = sum(int(row.get("trade_count") or 0) for row in rows)
    pnl = round(sum(float(row.get("simulated_pnl") or 0) for row in rows), 4)
    returns = [row.get("average_return") for row in rows if row.get("average_return") is not None and int(row.get("trade_count") or 0) > 0]
    return {
        "trade_count": trade_count,
        "simulated_pnl": pnl,
        "win_rate": _weighted_average(rows, "win_rate"),
        "max_drawdown": round(sum(float(row.get("max_drawdown") or 0) for row in rows), 4),
        "average_return": round(mean([float(value) for value in returns]), 4) if returns else 0.0,
        "average_hold_time": _weighted_average(rows, "average_hold_time_seconds"),
    }


def _weighted_average(rows: list[dict[str, Any]], key: str) -> float | None:
    total_weight = 0
    total = 0.0
    for row in rows:
        value = row.get(key)
        weight = int(row.get("trade_count") or 0)
        if value is None or weight <= 0:
            continue
        total += float(value) * weight
        total_weight += weight
    return round(total / total_weight, 4) if total_weight else None


def _variant(edge: dict[str, Any], name: str) -> dict[str, Any]:
    for row in edge.get("variant_rankings", []):
        if row.get("variant") == name:
            return row
    return {"variant": name, "trade_count": 0, "pnl": 0, "win_rate": None, "average_return": 0, "max_drawdown": 0}


def _confidence(sample: int, win_rate: Any, average_return: Any, min_sample_size: int) -> float:
    sample_score = min(1.0, sample / max(min_sample_size, 1))
    win_score = 0.5 if win_rate is None else max(0.0, min(1.0, float(win_rate)))
    return_score = max(0.0, min(1.0, 0.5 + float(average_return or 0)))
    return round((sample_score * 0.5) + (win_score * 0.3) + (return_score * 0.2), 4)


def _load_json(path: str | Path) -> dict[str, Any]:
    target = Path(path)
    if not target.exists():
        return {"available": False, "reason": f"{target} not found"}
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {"available": False, "reason": f"could not read {target}: {exc}"}
    if isinstance(payload, dict):
        payload.setdefault("available", True)
        return payload
    return {"available": False, "reason": f"{target} did not contain a JSON object"}


def _snapshot_summary(path: str | Path) -> dict[str, Any]:
    target = Path(path)
    if not target.exists():
        return {"available": False, "price_points": 0, "tickers": 0}
    if target.suffix.lower() not in {".db", ".sqlite", ".sqlite3"}:
        return {"available": True, "price_points": None, "tickers": None}
    with sqlite3.connect(target) as conn:
        try:
            count = conn.execute("SELECT COUNT(*) FROM kalshi_price_series").fetchone()[0]
            tickers = conn.execute("SELECT COUNT(DISTINCT ticker) FROM kalshi_price_series").fetchone()[0]
        except sqlite3.OperationalError:
            return {"available": False, "price_points": 0, "tickers": 0}
    return {"available": True, "price_points": count, "tickers": tickers}


def _input_warnings(
    account: dict[str, Any],
    edge: dict[str, Any],
    regime: dict[str, Any],
    snapshot: dict[str, Any],
    account_path: str | Path,
    edge_path: str | Path,
    regime_path: str | Path,
    snapshot_path: str | Path,
) -> list[str]:
    warnings = []
    if not account.get("available"):
        warnings.append(f"account_history_missing: {account_path}")
    if not edge.get("available", False):
        warnings.append(f"edge_analysis_missing: {edge_path}")
    if not regime.get("available", False):
        warnings.append(f"regime_analysis_missing: {regime_path}")
    if not snapshot.get("available"):
        warnings.append(f"snapshot_db_missing_or_empty: {snapshot_path}")
    return warnings


def _recommended_next_data(warnings: list[str], ranked: list[dict[str, Any]], min_sample_size: int) -> list[str]:
    recs = []
    if any("account_history_missing" in warning for warning in warnings):
        recs.append("export Kalshi account history with kalshi-export-account-history")
    if any("edge_analysis_missing" in warning for warning in warnings):
        recs.append("run kalshi-edge-analysis with --export")
    if any("regime_analysis_missing" in warning for warning in warnings):
        recs.append("run market-regime-analysis with --json or default export")
    if any("snapshot_db_missing" in warning for warning in warnings):
        recs.append("record more Kalshi market snapshots")
    if any(row["trade_count"] < min_sample_size for row in ranked):
        recs.append(f"collect at least {min_sample_size} qualifying samples per candidate strategy")
    return recs or ["continue collecting snapshots across trending, ranging, and choppy crypto regimes"]


def _avg(values: list[Any]) -> float | None:
    clean = [float(value) for value in values if value is not None]
    return round(mean(clean), 4) if clean else None
