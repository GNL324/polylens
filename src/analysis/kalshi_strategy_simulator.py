from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from pathlib import Path
from statistics import mean
from typing import Any

from src.analysis.kalshi_account_analytics import build_kalshi_account_report

CRYPTO_ASSETS = ("BTC", "ETH", "SOL", "XRP", "DOGE", "LTC", "ADA", "AVAX")
STRATEGY_MODES = {"extreme-probability", "mean-reversion", "momentum", "no-trade-baseline"}


@dataclass(frozen=True)
class SimulationConfig:
    assets: set[str] | None = None
    market_types: set[str] | None = None
    price_bands: list[tuple[float, float]] | None = None
    max_contracts: int = 1
    bankroll: float = 1000.0
    fee_assumption: float = 0.0
    strategy_mode: str = "extreme-probability"


def parse_csv_filter(value: str | None) -> set[str] | None:
    if not value:
        return None
    return {part.strip().upper() for part in value.split(",") if part.strip()}


def parse_price_bands(value: str | None) -> list[tuple[float, float]] | None:
    if not value:
        return None
    bands: list[tuple[float, float]] = []
    for raw in value.split(","):
        item = raw.strip()
        if not item:
            continue
        if "-" not in item:
            raise ValueError("price bands must use min-max format, e.g. 0.01-0.10")
        left, right = item.split("-", 1)
        low = _normalize_price(left)
        high = _normalize_price(right)
        if low > high:
            raise ValueError("price band minimum cannot exceed maximum")
        bands.append((low, high))
    return bands or None


def simulate_kalshi_strategy(balance: dict[str, Any] | None, positions_payload: dict[str, Any] | None, orders_payload: dict[str, Any] | None, fills_payload: dict[str, Any] | None, config: SimulationConfig) -> dict[str, Any]:
    if config.strategy_mode not in STRATEGY_MODES:
        raise ValueError(f"strategy mode must be one of {sorted(STRATEGY_MODES)}")
    account_report = build_kalshi_account_report(balance, positions_payload, orders_payload, fills_payload)
    history = account_report.get("fills") or _filled_orders(account_report.get("orders", []))
    normalized = [_normalize_trade(row) for row in history]
    filtered = [row for row in normalized if _passes_filters(row, config)]
    selected = [row for row in filtered if _strategy_accepts(row, config.strategy_mode)]
    simulated_trades = [_simulate_trade(row, config) for row in selected]
    equity_curve = _equity_curve(simulated_trades)
    pnl = round(sum(row["simulated_pnl"] for row in simulated_trades), 4)
    fees = round(sum(row["simulated_fee"] for row in simulated_trades), 4)
    wins = sum(1 for row in simulated_trades if row["simulated_pnl"] > 0)
    losses = sum(1 for row in simulated_trades if row["simulated_pnl"] < 0)
    avg_size = round(mean([row["contracts"] for row in simulated_trades]), 4) if simulated_trades else 0
    best = max(simulated_trades, key=lambda row: row["simulated_pnl"], default=None)
    worst = min(simulated_trades, key=lambda row: row["simulated_pnl"], default=None)
    summary = {
        "strategy_mode": config.strategy_mode,
        "history_trades": len(history),
        "filtered_trades": len(filtered),
        "simulated_trades": len(simulated_trades),
        "bankroll": config.bankroll,
        "simulated_pnl": pnl,
        "fees": fees,
        "net_pnl": round(pnl - fees, 4),
        "roi": round((pnl - fees) / config.bankroll, 4) if config.bankroll else None,
        "win_count": wins,
        "loss_count": losses,
        "win_rate": round(wins / (wins + losses), 4) if wins + losses else None,
        "max_drawdown": _max_drawdown(equity_curve),
        "average_trade_size": avg_size,
        "best_trade": best,
        "worst_trade": worst,
        "strategy_classification": _classify_strategy(config.strategy_mode, simulated_trades, pnl - fees),
        "enough_data_to_automate_safely": len(simulated_trades) >= 100 and wins + losses >= 50,
    }
    return {
        "summary": summary,
        "config": {
            "assets": sorted(config.assets) if config.assets else None,
            "market_types": sorted(config.market_types) if config.market_types else None,
            "price_bands": config.price_bands,
            "max_contracts": config.max_contracts,
            "bankroll": config.bankroll,
            "fee_assumption": config.fee_assumption,
            "strategy_mode": config.strategy_mode,
        },
        "account_summary": account_report.get("summary", {}),
        "trades": simulated_trades,
    }


def compare_kalshi_strategies(balance: dict[str, Any] | None, positions: dict[str, Any] | None, orders: dict[str, Any] | None, fills: dict[str, Any] | None, base_config: SimulationConfig) -> list[dict[str, Any]]:
    results = []
    for mode in sorted(STRATEGY_MODES):
        config = SimulationConfig(assets=base_config.assets, market_types=base_config.market_types, price_bands=base_config.price_bands, max_contracts=base_config.max_contracts, bankroll=base_config.bankroll, fee_assumption=base_config.fee_assumption, strategy_mode=mode)
        result = simulate_kalshi_strategy(balance, positions, orders, fills, config)
        results.append(result["summary"])
    return sorted(results, key=lambda row: (row.get("net_pnl") or 0, row.get("simulated_trades") or 0), reverse=True)


def export_kalshi_simulation(result: dict[str, Any], report_dir: str | Path = "data/reports") -> dict[str, str]:
    target = Path(report_dir)
    target.mkdir(parents=True, exist_ok=True)
    json_path = target / "kalshi_simulation.json"
    csv_path = target / "kalshi_simulation.csv"
    json_path.write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        fieldnames = ["ticker", "asset", "market_type", "side", "price", "contracts", "simulated_pnl", "simulated_fee", "reason"]
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in result.get("trades", []):
            writer.writerow({key: row.get(key) for key in fieldnames})
    return {"json": str(json_path), "csv": str(csv_path)}


def _normalize_trade(row: dict[str, Any]) -> dict[str, Any]:
    price = _extract_price(row)
    return {
        "ticker": _ticker(row),
        "title": str(row.get("title") or row.get("market_title") or row.get("subtitle") or _ticker(row)),
        "asset": _asset(row),
        "market_type": _market_type(row),
        "side": str(row.get("side") or row.get("action") or row.get("yes_no") or "unknown").lower(),
        "price": price,
        "contracts": _contracts(row),
        "pnl": _extract_pnl(row),
        "fee": _money(row.get("fee") or row.get("fee_dollars") or row.get("fees") or row.get("exchange_fee")) or 0,
        "raw": {"ticker": _ticker(row), "status": row.get("status")},
    }


def _passes_filters(row: dict[str, Any], config: SimulationConfig) -> bool:
    if config.assets and row["asset"].upper() not in config.assets:
        return False
    if config.market_types:
        allowed = {item.lower() for item in config.market_types}
        if row["market_type"].lower() not in allowed:
            return False
    if config.price_bands:
        price = row.get("price")
        if price is None or not any(low <= price <= high for low, high in config.price_bands):
            return False
    return True


def _strategy_accepts(row: dict[str, Any], mode: str) -> bool:
    price = row.get("price")
    if mode == "no-trade-baseline":
        return False
    if price is None:
        return False
    if mode == "extreme-probability":
        return price <= 0.1 or price >= 0.9
    if mode == "mean-reversion":
        return 0.35 <= price <= 0.65
    if mode == "momentum":
        return 0.65 < price < 0.9
    return False


def _simulate_trade(row: dict[str, Any], config: SimulationConfig) -> dict[str, Any]:
    contracts = min(max(int(row.get("contracts") or 0), 1), max(int(config.max_contracts), 1))
    pnl = row.get("pnl")
    if pnl is None:
        pnl = _proxy_pnl(row, contracts)
    else:
        original_contracts = max(float(row.get("contracts") or contracts), 1)
        pnl = float(pnl) * (contracts / original_contracts)
    fee = abs(float(config.fee_assumption)) * contracts + float(row.get("fee") or 0) * (contracts / max(float(row.get("contracts") or contracts), 1))
    reason = f"{config.strategy_mode} accepted price {row.get('price')}"
    return {**row, "contracts": contracts, "simulated_pnl": round(pnl, 4), "simulated_fee": round(fee, 4), "reason": reason}


def _proxy_pnl(row: dict[str, Any], contracts: int) -> float:
    price = row.get("price")
    if price is None:
        return 0.0
    side = row.get("side")
    if side in {"yes", "buy"}:
        return (1 - price) * contracts if price >= 0.5 else -price * contracts
    return price * contracts if price <= 0.5 else -(1 - price) * contracts


def _equity_curve(trades: list[dict[str, Any]]) -> list[float]:
    total = 0.0
    curve = []
    for trade in trades:
        total += trade["simulated_pnl"] - trade["simulated_fee"]
        curve.append(round(total, 4))
    return curve


def _max_drawdown(curve: list[float]) -> float:
    peak = 0.0
    max_dd = 0.0
    for value in curve:
        peak = max(peak, value)
        max_dd = min(max_dd, value - peak)
    return round(abs(max_dd), 4)


def _classify_strategy(mode: str, trades: list[dict[str, Any]], net_pnl: float) -> str:
    if not trades:
        return "no_trade_baseline" if mode == "no-trade-baseline" else "insufficient_matching_history"
    if net_pnl > 0:
        return "historically_profitable_simulation"
    if net_pnl < 0:
        return "historically_unprofitable_simulation"
    return "flat_simulation"


def _filled_orders(orders: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [order for order in orders if str(order.get("status", "")).lower() in {"filled", "executed", "partially_filled", "filled_paper"}]


def _ticker(row: dict[str, Any]) -> str:
    return str(row.get("ticker") or row.get("market_ticker") or row.get("market_id") or "unknown")


def _money(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    if float(numeric).is_integer() and abs(numeric) >= 1:
        return round(numeric / 100.0, 4)
    return round(numeric, 4)


def _extract_price(row: dict[str, Any]) -> float | None:
    for key in ("price", "yes_price", "no_price", "fill_price", "avg_price", "average_price", "price_dollars"):
        if row.get(key) not in (None, ""):
            return _normalize_price(row.get(key))
    return None


def _normalize_price(value: Any) -> float:
    numeric = float(value)
    if numeric > 1:
        numeric /= 100.0
    if numeric < 0 or numeric > 1:
        raise ValueError("price must normalize to 0..1")
    return round(numeric, 4)


def _contracts(row: dict[str, Any]) -> float:
    for key in ("count", "contracts", "quantity", "qty", "size", "fill_count", "filled_count"):
        if row.get(key) not in (None, ""):
            try:
                return abs(float(row.get(key)))
            except (TypeError, ValueError):
                return 0.0
    return 0.0


def _extract_pnl(row: dict[str, Any]) -> float | None:
    for key in ("realized_pnl", "realized_pnl_dollars", "pnl", "profit", "profit_dollars"):
        if row.get(key) not in (None, ""):
            return _money(row.get(key))
    return None


def _asset(row: dict[str, Any]) -> str:
    text = " ".join(str(row.get(key) or "") for key in ("asset", "ticker", "event_ticker", "title", "market_title", "subtitle")).upper()
    for symbol in CRYPTO_ASSETS:
        if symbol in text or f"KX{symbol}" in text:
            return symbol
    if "BITCOIN" in text:
        return "BTC"
    if "ETHEREUM" in text:
        return "ETH"
    if "SOLANA" in text:
        return "SOL"
    return "unknown"


def _market_type(row: dict[str, Any]) -> str:
    text = " ".join(str(row.get(key) or "") for key in ("market_type", "title", "market_title", "ticker", "event_ticker")).lower()
    if any(token in text for token in ("btc", "eth", "sol", "xrp", "doge", "bitcoin", "ethereum", "crypto")):
        return "crypto"
    if any(token in text for token in ("nba", "nfl", "mlb", "nhl", "championship", "finals")):
        return "sports"
    return "unknown"
