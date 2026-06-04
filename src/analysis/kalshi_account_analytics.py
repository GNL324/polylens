from __future__ import annotations

import csv
import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from statistics import mean
from typing import Any

CRYPTO_ASSETS = {
    "BTC": ("BTC", "BITCOIN", "KXBTC"),
    "ETH": ("ETH", "ETHEREUM", "KXETH"),
    "SOL": ("SOL", "SOLANA", "KXSOL"),
    "XRP": ("XRP", "RIPPLE", "KXXRP"),
    "DOGE": ("DOGE", "DOGECOIN", "KXDOGE"),
    "LTC": ("LTC", "LITECOIN", "KXLTC"),
    "ADA": ("ADA", "CARDANO"),
    "AVAX": ("AVAX", "AVALANCHE"),
}


def build_kalshi_account_report(balance: dict[str, Any] | None, positions_payload: dict[str, Any] | None, orders_payload: dict[str, Any] | None, fills_payload: dict[str, Any] | None) -> dict[str, Any]:
    positions = _rows(positions_payload, "positions")
    orders = _rows(orders_payload, "orders")
    fills = _rows(fills_payload, "fills")
    trades = fills or _filled_orders(orders)
    realized_pnl = _sum_fields(positions, ["realized_pnl", "realized_pnl_dollars", "realized_profit", "realized_profit_dollars"])
    fees_paid = _sum_fields(trades, ["fee", "fee_dollars", "fees", "fees_dollars", "exchange_fee", "exchange_fee_dollars"])
    open_positions = [row for row in positions if _position_size(row) != 0]
    trade_count = len(trades)
    contract_sizes = [_contracts(row) for row in trades if _contracts(row) > 0]
    entry_prices = [_price(row) for row in trades if _price(row) is not None]
    outcomes = [_trade_outcome(row) for row in trades]
    wins = sum(1 for outcome in outcomes if outcome == "win")
    losses = sum(1 for outcome in outcomes if outcome == "loss")
    win_rate = round(wins / (wins + losses), 4) if wins + losses else None
    summary = {
        "account_balance": _balance_amount(balance or {}),
        "open_position_count": len(open_positions),
        "realized_pnl": round(realized_pnl, 4),
        "fees_paid": round(fees_paid, 4),
        "trade_count": trade_count,
        "win_rate": win_rate,
        "average_entry_price": round(mean(entry_prices), 4) if entry_prices else None,
        "average_contract_size": round(mean(contract_sizes), 4) if contract_sizes else None,
    }
    report = {
        "summary": summary,
        "balance": balance or {},
        "open_positions": open_positions,
        "trades_by_market_type": dict(Counter(_market_type(row) for row in trades)),
        "trades_by_asset": dict(Counter(_asset(row) for row in trades)),
        "top_traded_markets": _top_traded_markets(trades),
        "fee_summary": {"fees_paid": summary["fees_paid"], "average_fee_per_trade": round(fees_paid / trade_count, 4) if trade_count else 0},
        "pnl_summary": {
            "realized_pnl": summary["realized_pnl"],
            "position_unrealized_pnl": round(_sum_fields(positions, ["unrealized_pnl", "unrealized_pnl_dollars"]), 4),
            "winning_trades": wins,
            "losing_trades": losses,
            "win_rate": win_rate,
        },
        "trade_metrics": {
            "average_entry_price": summary["average_entry_price"],
            "average_contract_size": summary["average_contract_size"],
            "fills_used_for_trade_metrics": bool(fills),
        },
        "positions": positions,
        "orders": orders,
        "fills": fills,
    }
    report["patterns"] = detect_kalshi_patterns(report)
    return report


def detect_kalshi_patterns(report: dict[str, Any]) -> dict[str, Any]:
    trades = report.get("fills") or _filled_orders(report.get("orders", []))
    market_counts = Counter(_ticker(row) for row in trades if _ticker(row))
    market_types = Counter(_market_type(row) for row in trades)
    assets = Counter(_asset(row) for row in trades)
    roi_rows = _roi_rows(trades)
    high_roi = sorted([row for row in roi_rows if row.get("roi") is not None and row["roi"] > 0], key=lambda row: row["roi"], reverse=True)[:10]
    worst = sorted([row for row in roi_rows if row.get("roi") is not None], key=lambda row: row["roi"])[:10]
    scalp = _scalp_patterns(trades)
    arbs = _possible_arbitrage_patterns(trades)
    repeated = [{"market": market, "trade_count": count} for market, count in market_counts.most_common(10) if count >= 2]
    behavior = _classify_behavior(market_counts, market_types, scalp, arbs)
    return {
        "repeated_trading_behavior": repeated,
        "markets_traded_most_frequently": [{"market": market, "trade_count": count} for market, count in market_counts.most_common(10)],
        "high_roi_trades": high_roi,
        "worst_trades": worst,
        "scalp_patterns": scalp,
        "possible_arbitrage_behavior": arbs,
        "behavior_classification": behavior["label"],
        "behavior_reasons": behavior["reasons"],
        "top_assets": dict(assets.most_common(10)),
        "top_market_types": dict(market_types.most_common(10)),
    }


def export_kalshi_report(report: dict[str, Any], report_dir: str | Path = "data/reports") -> dict[str, str]:
    target = Path(report_dir)
    target.mkdir(parents=True, exist_ok=True)
    json_path = target / "kalshi_report.json"
    csv_path = target / "kalshi_report.csv"
    json_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["section", "name", "value"])
        writer.writeheader()
        writer.writerows(_csv_rows(report))
    return {"json": str(json_path), "csv": str(csv_path)}


def _rows(payload: Any, key: str) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if not isinstance(payload, dict):
        return []
    rows = payload.get(key)
    if isinstance(rows, list):
        return [row for row in rows if isinstance(row, dict)]
    for fallback in ("items", "data", "results"):
        value = payload.get(fallback)
        if isinstance(value, list):
            return [row for row in value if isinstance(row, dict)]
    return []


def _filled_orders(orders: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [order for order in orders if str(order.get("status", "")).lower() in {"filled", "executed", "partially_filled", "filled_paper"}]


def _money(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    # Kalshi API monetary fields commonly arrive as integer cents. Decimal
    # strings such as "0.45" are already dollars/probability units.
    if float(numeric).is_integer() and abs(numeric) > 1:
        return round(numeric / 100.0, 4)
    return round(numeric, 4)


def _balance_amount(balance: dict[str, Any]) -> float | None:
    for key in ("balance_dollars", "available_balance_dollars", "cash_dollars", "balance", "available_balance", "cash"):
        if balance.get(key) not in (None, ""):
            return _money(balance.get(key))
    return None


def _sum_fields(rows: list[dict[str, Any]], fields: list[str]) -> float:
    total = 0.0
    for row in rows:
        for field in fields:
            if row.get(field) not in (None, ""):
                total += _money(row.get(field)) or 0
                break
    return total


def _price(row: dict[str, Any]) -> float | None:
    for key in ("price", "yes_price", "no_price", "fill_price", "avg_price", "average_price", "price_dollars"):
        if row.get(key) not in (None, ""):
            value = _money(row.get(key))
            if value is not None and value > 1 and value <= 100:
                value = value / 100
            return round(value, 4) if value is not None else None
    return None


def _contracts(row: dict[str, Any]) -> float:
    for key in ("count", "contracts", "quantity", "qty", "size", "fill_count", "filled_count"):
        if row.get(key) not in (None, ""):
            try:
                return abs(float(row.get(key)))
            except (TypeError, ValueError):
                return 0.0
    return 0.0


def _position_size(row: dict[str, Any]) -> float:
    for key in ("position", "contracts", "quantity", "count", "yes_count", "no_count"):
        if row.get(key) not in (None, ""):
            try:
                return float(row.get(key))
            except (TypeError, ValueError):
                continue
    return 0.0


def _ticker(row: dict[str, Any]) -> str:
    return str(row.get("ticker") or row.get("market_ticker") or row.get("market_id") or "unknown")


def _title(row: dict[str, Any]) -> str:
    return str(row.get("title") or row.get("market_title") or row.get("subtitle") or _ticker(row))


def _market_type(row: dict[str, Any]) -> str:
    text = " ".join(str(row.get(key) or "") for key in ("market_type", "title", "market_title", "ticker", "event_ticker")).lower()
    if any(word in text for word in ("btc", "eth", "sol", "xrp", "doge", "bitcoin", "ethereum", "crypto")):
        if any(word in text for word in ("above", "below", "higher", "lower", "up", "down")):
            return "crypto_price_direction"
        return "crypto"
    if any(word in text for word in ("nba", "nfl", "mlb", "nhl", "championship", "finals", "super bowl", "world series")):
        return "sports_outright" if any(word in text for word in ("championship", "finals", "super bowl", "world series")) else "sports"
    if any(word in text for word in ("fed", "inflation", "cpi", "rates", "election")):
        return "macro"
    return "unknown"


def _asset(row: dict[str, Any]) -> str:
    text = " ".join(str(row.get(key) or "") for key in ("asset", "ticker", "event_ticker", "title", "market_title", "subtitle")).upper()
    for symbol, tokens in CRYPTO_ASSETS.items():
        if any(token in text for token in tokens):
            return symbol
    return "unknown"


def _trade_outcome(row: dict[str, Any]) -> str:
    pnl = _trade_pnl(row)
    if pnl is None:
        status = str(row.get("status") or row.get("result") or "").lower()
        if status in {"won", "win"}:
            return "win"
        if status in {"lost", "loss"}:
            return "loss"
        return "unknown"
    if pnl > 0:
        return "win"
    if pnl < 0:
        return "loss"
    return "push"


def _trade_cost(row: dict[str, Any]) -> float | None:
    for key in ("cost", "cost_dollars", "trade_value", "trade_value_dollars"):
        if row.get(key) not in (None, ""):
            return abs(_money(row.get(key)) or 0)
    price = _price(row)
    contracts = _contracts(row)
    return abs(price * contracts) if price is not None and contracts else None


def _trade_pnl(row: dict[str, Any]) -> float | None:
    for key in ("realized_pnl", "realized_pnl_dollars", "pnl", "profit", "profit_dollars"):
        if row.get(key) not in (None, ""):
            return _money(row.get(key))
    return None


def _roi_rows(trades: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for trade in trades:
        cost = _trade_cost(trade)
        pnl = _trade_pnl(trade)
        rows.append({
            "ticker": _ticker(trade),
            "title": _title(trade),
            "asset": _asset(trade),
            "market_type": _market_type(trade),
            "side": str(trade.get("side") or trade.get("action") or trade.get("yes_no") or "unknown").lower(),
            "price": _price(trade),
            "contracts": _contracts(trade),
            "pnl": pnl,
            "roi": round(pnl / cost, 4) if cost and pnl is not None else None,
        })
    return rows


def _top_traded_markets(trades: list[dict[str, Any]]) -> list[dict[str, Any]]:
    counts = Counter(_ticker(row) for row in trades if _ticker(row))
    titles = {}
    for row in trades:
        titles.setdefault(_ticker(row), _title(row))
    return [{"ticker": ticker, "title": titles.get(ticker, ticker), "trade_count": count} for ticker, count in counts.most_common(10)]


def _scalp_patterns(trades: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for trade in trades:
        grouped[_ticker(trade)].append(trade)
    patterns = []
    for ticker, rows in grouped.items():
        prices = [_price(row) for row in rows if _price(row) is not None]
        sides = {str(row.get("side") or row.get("action") or "").lower() for row in rows}
        if len(rows) >= 4 and len(sides) >= 2:
            patterns.append({"ticker": ticker, "trade_count": len(rows), "reason": "multiple entries and exits on both sides"})
        elif len(prices) >= 4 and max(prices) - min(prices) <= 0.08:
            patterns.append({"ticker": ticker, "trade_count": len(rows), "reason": "frequent small-range price trading"})
    return patterns[:10]


def _possible_arbitrage_patterns(trades: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for trade in trades:
        grouped[_ticker(trade)].append(trade)
    patterns = []
    for ticker, rows in grouped.items():
        sides = {str(row.get("side") or row.get("action") or row.get("yes_no") or "").lower() for row in rows}
        if {"yes", "no"}.issubset(sides) or {"buy", "sell"}.issubset(sides):
            patterns.append({"ticker": ticker, "trade_count": len(rows), "reason": "opposing-side activity detected"})
    return patterns[:10]


def _classify_behavior(market_counts: Counter[str], market_types: Counter[str], scalp_patterns: list[dict[str, Any]], possible_arbs: list[dict[str, Any]]) -> dict[str, Any]:
    reasons = []
    if possible_arbs:
        return {"label": "possible_arbitrage", "reasons": ["opposing-side activity appears in account history"]}
    if scalp_patterns:
        return {"label": "market_making_or_scalping", "reasons": ["frequent same-market entries and exits"]}
    if market_counts and market_counts.most_common(1)[0][1] >= 5:
        return {"label": "directional_speculation", "reasons": ["repeat trading concentrated in a small number of markets"]}
    if market_types:
        reasons.append(f"most common market type is {market_types.most_common(1)[0][0]}")
    return {"label": "mixed_or_insufficient_history", "reasons": reasons}


def _csv_rows(report: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for key, value in (report.get("summary") or {}).items():
        rows.append({"section": "summary", "name": key, "value": value})
    for key, value in (report.get("trades_by_market_type") or {}).items():
        rows.append({"section": "trades_by_market_type", "name": key, "value": value})
    for key, value in (report.get("trades_by_asset") or {}).items():
        rows.append({"section": "trades_by_asset", "name": key, "value": value})
    patterns = report.get("patterns") or {}
    rows.append({"section": "patterns", "name": "behavior_classification", "value": patterns.get("behavior_classification")})
    for item in patterns.get("markets_traded_most_frequently", []):
        rows.append({"section": "top_markets", "name": item.get("market"), "value": item.get("trade_count")})
    return rows
