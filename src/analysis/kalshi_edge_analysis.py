from __future__ import annotations

import csv
import json
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean
from typing import Any

from src.analysis.kalshi_account_history_export import DEFAULT_ACCOUNT_HISTORY_PATH, load_kalshi_account_history
from src.storage.kalshi_market_data import DEFAULT_KALSHI_DATA_DB


DEFAULT_EDGE_REPORT_JSON = "data/reports/kalshi_edge_analysis.json"
DEFAULT_EDGE_REPORT_CSV = "data/reports/kalshi_edge_analysis.csv"


def run_kalshi_edge_analysis(
    *,
    account_history_path: str | Path = DEFAULT_ACCOUNT_HISTORY_PATH,
    snapshot_path: str | Path = DEFAULT_KALSHI_DATA_DB,
    export: bool = False,
) -> dict[str, Any]:
    history = load_kalshi_account_history(account_history_path)
    snapshots = _load_snapshots(snapshot_path)
    warnings: list[str] = []
    if not history.get("available"):
        warnings.append(f"account_history_missing: {history.get('reason')}")
    if not snapshots:
        warnings.append(f"snapshots_missing_or_empty: {snapshot_path}")

    payload = history.get("payload") or {}
    orders = _rows(payload.get("orders"), "orders")
    fills = _rows(payload.get("fills"), "fills")
    source_rows = fills or _filled_orders(orders)
    trades = [_normalize_trade(row) for row in source_rows]
    trades = [trade for trade in trades if trade["ticker"] and trade["price"] is not None and trade["contracts"] > 0]
    if not trades:
        warnings.append("insufficient_sample_size: no usable fills/orders in account history")
    elif len(trades) < 5:
        warnings.append(f"insufficient_sample_size: only {len(trades)} usable trades")

    indexed = _index_snapshots(snapshots)
    variants = {
        "actual_trades": [],
        "opposite_side_trades": [],
        "delayed_entry_1m": [],
        "delayed_entry_5m": [],
        "early_exit": [],
        "hold_to_expiry": [],
    }
    for trade in trades:
        for name, row in _variant_results(trade, indexed).items():
            if row is not None:
                variants[name].append(row)

    ranked = sorted((_variant_metrics(name, rows) for name, rows in variants.items()), key=lambda row: (row["pnl"], row["win_rate"] or -1, -row["max_drawdown"]), reverse=True)
    for index, row in enumerate(ranked, start=1):
        row["rank"] = index
    classification = _classify(ranked)
    result = {
        "summary": {
            "classification": classification["classification"],
            "edge_classification": classification["classification"],
            "has_directional_edge": classification["has_directional_edge"],
            "has_inverted_edge": classification["has_inverted_edge"],
            "has_timing_problem": classification["has_timing_problem"],
            "has_no_detectable_edge": classification["has_no_detectable_edge"],
            "sample_size": len(trades),
            "snapshot_count": len(snapshots),
            "confidence_level": _confidence_level(len(trades)),
            "best_variant": ranked[0]["variant"] if ranked else None,
            "data_quality_warnings": warnings,
        },
        "inputs": {
            "account_history_path": str(account_history_path),
            "snapshot_path": str(snapshot_path),
            "account_history_available": bool(history.get("available")),
        },
        "variant_rankings": ranked,
        "variants": variants,
        "data_quality_warnings": warnings,
    }
    if export:
        result["files"] = export_kalshi_edge_analysis(result)
    return result


def export_kalshi_edge_analysis(report: dict[str, Any], report_dir: str | Path = "data/reports") -> dict[str, str]:
    target = Path(report_dir)
    target.mkdir(parents=True, exist_ok=True)
    json_path = target / "kalshi_edge_analysis.json"
    csv_path = target / "kalshi_edge_analysis.csv"
    json_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        fields = ["rank", "variant", "trade_count", "pnl", "win_rate", "average_return", "max_drawdown", "fees", "confidence_level"]
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in report.get("variant_rankings", []):
            writer.writerow({key: row.get(key) for key in fields})
    return {"json": str(json_path), "csv": str(csv_path)}


def _variant_results(trade: dict[str, Any], snapshots: dict[str, list[dict[str, Any]]]) -> dict[str, dict[str, Any] | None]:
    rows = snapshots.get(trade["ticker"], [])
    return {
        "actual_trades": _settled_result(trade, "actual_trades"),
        "hold_to_expiry": _settled_result(trade, "hold_to_expiry"),
        "opposite_side_trades": _settled_result({**trade, "side": _opposite(trade["side"]), "price": round(1 - trade["price"], 4)}, "opposite_side_trades"),
        "delayed_entry_1m": _delayed_entry(trade, rows, 60, "delayed_entry_1m"),
        "delayed_entry_5m": _delayed_entry(trade, rows, 300, "delayed_entry_5m"),
        "early_exit": _early_exit(trade, rows, 300),
    }


def _settled_result(trade: dict[str, Any], variant: str) -> dict[str, Any] | None:
    if trade.get("pnl") is not None and variant == "actual_trades":
        pnl = float(trade["pnl"]) - trade["fee"]
        return _result(trade, variant, pnl, trade["price"], None)
    outcome = trade.get("outcome")
    if outcome not in {"yes", "no"}:
        return None
    payout = 1.0 if trade["side"] == outcome else 0.0
    pnl = (payout - trade["price"]) * trade["contracts"] - trade["fee"]
    return _result(trade, variant, pnl, trade["price"], payout)


def _delayed_entry(trade: dict[str, Any], rows: list[dict[str, Any]], seconds: int, variant: str) -> dict[str, Any] | None:
    if trade.get("timestamp") is None:
        return None
    snapshot = _snapshot_at_or_after(rows, trade["timestamp"] + seconds)
    if not snapshot:
        return None
    price = _entry_price(snapshot, trade["side"])
    if price is None:
        return None
    return _settled_result({**trade, "price": price}, variant)


def _early_exit(trade: dict[str, Any], rows: list[dict[str, Any]], seconds: int) -> dict[str, Any] | None:
    if trade.get("timestamp") is None:
        return None
    snapshot = _snapshot_at_or_after(rows, trade["timestamp"] + seconds)
    if not snapshot:
        return None
    exit_price = _exit_price(snapshot, trade["side"])
    if exit_price is None:
        return None
    pnl = (exit_price - trade["price"]) * trade["contracts"] - trade["fee"]
    return _result(trade, "early_exit", pnl, trade["price"], exit_price)


def _result(trade: dict[str, Any], variant: str, pnl: float, entry_price: float, exit_price: float | None) -> dict[str, Any]:
    stake = max(entry_price * trade["contracts"], 0.01)
    return {
        "variant": variant,
        "ticker": trade["ticker"],
        "side": trade["side"],
        "entry_price": round(entry_price, 4),
        "exit_price": round(exit_price, 4) if exit_price is not None else None,
        "contracts": trade["contracts"],
        "pnl": round(pnl, 4),
        "fees": trade["fee"],
        "return": round(pnl / stake, 4),
        "timestamp": trade.get("timestamp"),
    }


def _variant_metrics(name: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    pnls = [row["pnl"] for row in rows]
    wins = sum(1 for pnl in pnls if pnl > 0)
    losses = sum(1 for pnl in pnls if pnl < 0)
    return {
        "variant": name,
        "trade_count": len(rows),
        "pnl": round(sum(pnls), 4),
        "win_rate": round(wins / (wins + losses), 4) if wins + losses else None,
        "average_return": round(mean([row["return"] for row in rows]), 4) if rows else 0.0,
        "max_drawdown": _max_drawdown(pnls),
        "fees": round(sum(row["fees"] for row in rows), 4),
        "confidence_level": _confidence_level(len(rows)),
    }


def _classify(ranked: list[dict[str, Any]]) -> dict[str, Any]:
    by_name = {row["variant"]: row for row in ranked}
    actual = by_name.get("actual_trades", {})
    opposite = by_name.get("opposite_side_trades", {})
    delayed = [row for row in ranked if row["variant"].startswith("delayed_entry_") or row["variant"] == "early_exit"]
    sample = int(actual.get("trade_count") or 0)
    actual_pnl = float(actual.get("pnl") or 0)
    opposite_pnl = float(opposite.get("pnl") or 0)
    best_timing = max((float(row.get("pnl") or 0) for row in delayed), default=0.0)
    directional = sample >= 5 and actual_pnl > 0 and actual_pnl >= opposite_pnl
    inverted = sample >= 5 and opposite_pnl > max(actual_pnl, 0)
    timing = sample >= 5 and best_timing > max(actual_pnl, 0)
    if sample < 5:
        label = "insufficient_data"
    elif directional:
        label = "directional_edge"
    elif inverted:
        label = "inverted_edge"
    elif timing:
        label = "timing_problem"
    else:
        label = "no_detectable_edge"
    return {
        "classification": label,
        "has_directional_edge": directional,
        "has_inverted_edge": inverted,
        "has_timing_problem": timing,
        "has_no_detectable_edge": label in {"no_detectable_edge", "insufficient_data"},
    }


def _load_snapshots(path: str | Path) -> list[dict[str, Any]]:
    target = Path(path)
    if not target.exists():
        return []
    if target.suffix.lower() in {".json", ".jsonl", ".ndjson"}:
        return _load_json_snapshots(target)
    if target.suffix.lower() == ".csv":
        with target.open("r", encoding="utf-8", newline="") as handle:
            return [_normalize_snapshot(row) for row in csv.DictReader(handle)]
    if target.suffix.lower() in {".db", ".sqlite", ".sqlite3"}:
        return _load_sqlite_snapshots(target)
    return []


def _load_json_snapshots(path: Path) -> list[dict[str, Any]]:
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return []
    if path.suffix.lower() in {".jsonl", ".ndjson"}:
        return [_normalize_snapshot(json.loads(line)) for line in text.splitlines() if line.strip()]
    payload = json.loads(text)
    return [_normalize_snapshot(row) for row in _rows(payload, "snapshots")]


def _load_sqlite_snapshots(path: Path) -> list[dict[str, Any]]:
    rows = []
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        for table in ("kalshi_price_series", "kalshi_market_snapshots", "kalshi_orderbook_snapshots"):
            try:
                rows.extend(_normalize_snapshot(dict(row)) for row in conn.execute(f"SELECT * FROM {table} ORDER BY ticker, timestamp").fetchall())
            except sqlite3.OperationalError:
                continue
    return [row for row in rows if row["ticker"] and row["timestamp"] is not None]


def _normalize_snapshot(row: dict[str, Any]) -> dict[str, Any]:
    yes_bid = _price(row.get("yes_bid"))
    yes_ask = _price(row.get("yes_ask"))
    mid = _price(row.get("mid_price"))
    if mid is not None and yes_bid is None:
        yes_bid = mid
    if mid is not None and yes_ask is None:
        yes_ask = mid
    return {
        "ticker": str(row.get("ticker") or row.get("market_ticker") or ""),
        "timestamp": _timestamp(row.get("timestamp") or row.get("recorded_at") or row.get("created_at")),
        "yes_bid": yes_bid,
        "yes_ask": yes_ask,
        "no_bid": _price(row.get("no_bid")),
        "no_ask": _price(row.get("no_ask")),
    }


def _index_snapshots(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    indexed: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        indexed[row["ticker"]].append(row)
    for ticker in indexed:
        indexed[ticker].sort(key=lambda row: row["timestamp"] or 0)
    return indexed


def _normalize_trade(row: dict[str, Any]) -> dict[str, Any]:
    side = _side(row)
    return {
        "ticker": str(row.get("ticker") or row.get("market_ticker") or ""),
        "side": side,
        "price": _price(row.get("price") or row.get(f"{side}_price") or row.get("fill_price") or row.get("avg_price")),
        "contracts": _contracts(row),
        "fee": _money(row.get("fee") or row.get("fee_dollars") or row.get("fees") or row.get("exchange_fee")) or 0.0,
        "pnl": _money(row.get("realized_pnl") or row.get("realized_pnl_dollars") or row.get("pnl") or row.get("profit")),
        "outcome": _outcome(row, side),
        "timestamp": _timestamp(row.get("created_time") or row.get("created_at") or row.get("fill_time") or row.get("executed_at") or row.get("timestamp")),
    }


def _rows(payload: Any, key: str) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict):
        if isinstance(payload.get(key), list):
            return [row for row in payload[key] if isinstance(row, dict)]
        for fallback in ("data", "items", "results", "snapshots"):
            if isinstance(payload.get(fallback), list):
                return [row for row in payload[fallback] if isinstance(row, dict)]
    return []


def _filled_orders(orders: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [row for row in orders if str(row.get("status") or "").lower() in {"filled", "executed", "partially_filled"}]


def _side(row: dict[str, Any]) -> str:
    value = str(row.get("side") or row.get("yes_no") or row.get("contract_side") or "yes").lower()
    return "no" if value in {"no", "n"} else "yes"


def _opposite(side: str) -> str:
    return "no" if side == "yes" else "yes"


def _outcome(row: dict[str, Any], side: str) -> str | None:
    value = str(row.get("outcome") or row.get("result") or row.get("settlement_result") or row.get("winning_side") or "").lower()
    if value in {"yes", "y", "won_yes"}:
        return "yes"
    if value in {"no", "n", "won_no"}:
        return "no"
    if value in {"won", "win"}:
        return side
    if value in {"lost", "loss"}:
        return _opposite(side)
    return None


def _snapshot_at_or_after(rows: list[dict[str, Any]], timestamp: float) -> dict[str, Any] | None:
    return next((row for row in rows if row.get("timestamp") is not None and row["timestamp"] >= timestamp), None)


def _entry_price(row: dict[str, Any], side: str) -> float | None:
    if side == "yes":
        return row.get("yes_ask")
    return row.get("no_ask") if row.get("no_ask") is not None else _inverse(row.get("yes_bid"))


def _exit_price(row: dict[str, Any], side: str) -> float | None:
    if side == "yes":
        return row.get("yes_bid")
    return row.get("no_bid") if row.get("no_bid") is not None else _inverse(row.get("yes_ask"))


def _price(value: Any) -> float | None:
    if value in (None, ""):
        return None
    numeric = float(value)
    if numeric > 1:
        numeric /= 100.0
    return round(min(max(numeric, 0.0), 1.0), 4)


def _contracts(row: dict[str, Any]) -> float:
    for key in ("count", "contracts", "quantity", "qty", "size", "fill_count", "filled_count"):
        if row.get(key) not in (None, ""):
            return abs(float(row[key]))
    return 0.0


def _money(value: Any) -> float | None:
    if value in (None, ""):
        return None
    numeric = float(value)
    if abs(numeric) > 100 and float(numeric).is_integer():
        numeric /= 100.0
    return round(numeric, 4)


def _timestamp(value: Any) -> float | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        numeric = float(value)
        return numeric / 1000 if numeric > 10_000_000_000 else numeric
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _inverse(value: float | None) -> float | None:
    return round(1 - value, 4) if value is not None else None


def _max_drawdown(values: list[float]) -> float:
    total = 0.0
    peak = 0.0
    drawdown = 0.0
    for value in values:
        total += value
        peak = max(peak, total)
        drawdown = min(drawdown, total - peak)
    return round(abs(drawdown), 4)


def _confidence_level(sample_size: int) -> str:
    if sample_size < 5:
        return "insufficient_data"
    if sample_size < 20:
        return "low"
    if sample_size < 50:
        return "medium"
    return "high"
