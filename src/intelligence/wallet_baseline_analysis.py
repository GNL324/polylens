from __future__ import annotations

import random
import statistics
from typing import Any

from src.analysis.paper_copy_trader import DEFAULT_PAPER_COPY_DB, paper_copy_report
from src.analysis.trader_discovery import DEFAULT_TRADER_DISCOVERY_DB, load_discovered_wallets
from src.analysis.trader_registry import DEFAULT_TRADERS_DB, list_traders
from src.analysis.trader_signal_engine import DEFAULT_TRADER_SIGNAL_DB
from src.intelligence.wallet_discovery import WalletDiscoveryEngine
from src.intelligence.wallet_performance import WalletPerformanceEngine
from src.intelligence.wallet_signal_analytics import _wallet_validation_stats
from src.sqlite_utils import closing_connection


def _with_flags(payload: dict[str, Any]) -> dict[str, Any]:
    return {"read_only": True, "paper_only": True, **payload}


def _safe_float(value: Any, default: float = 0.0) -> float:
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _cohort_metrics(wallets: list[str], copy_by_wallet: dict[str, Any], validations: dict[str, dict[str, Any]]) -> dict[str, Any]:
    rois: list[float] = []
    win_rates: list[float] = []
    drawdowns: list[float] = []
    sharpes: list[float] = []
    for wallet in wallets:
        stats = copy_by_wallet.get(wallet, {})
        if stats.get("roi") is not None:
            rois.append(_safe_float(stats["roi"]))
        closed = int(stats.get("closed_positions") or 0)
        if closed > 0:
            wins = 0
            path_exists = bool(stats)
            if path_exists and stats.get("win_rate") is not None:
                win_rates.append(_safe_float(stats["win_rate"]))
        validation = validations.get(wallet, {})
        if validation.get("accuracy"):
            sharpes.append(_safe_float(validation.get("avg_roi_proxy")))
    expectancy = statistics.mean(rois) if rois else 0.0
    return {
        "wallet_count": len(wallets),
        "roi": round(statistics.mean(rois), 4) if rois else None,
        "expectancy": round(expectancy, 4),
        "win_rate": round(statistics.mean(win_rates), 4) if win_rates else None,
        "drawdown": round(abs(min(rois)), 4) if rois else None,
        "sharpe_like": round(statistics.mean(sharpes), 4) if sharpes else None,
    }


def wallet_baseline_analysis_report(
    *,
    traders_db_path: str = DEFAULT_TRADERS_DB,
    discovery_db_path: str = DEFAULT_TRADER_DISCOVERY_DB,
    paper_copy_db_path: str = DEFAULT_PAPER_COPY_DB,
    signal_db_path: str = DEFAULT_TRADER_SIGNAL_DB,
) -> dict[str, Any]:
    copy = paper_copy_report(db_path=paper_copy_db_path)
    copy_by_wallet = copy.get("by_wallet", {})
    validations = _wallet_validation_stats(signal_db_path)
    all_wallets = list(copy_by_wallet.keys())
    discovered = [c.wallet for c in load_discovered_wallets(db_path=discovery_db_path, limit=200)]
    discovery_engine = WalletDiscoveryEngine(traders_db_path=traders_db_path, discovery_db_path=discovery_db_path)
    promoted = [
        str(row["wallet"])
        for row in discovery_engine.load_lifecycle(state="active", limit=100)
    ]
    active = [
        str(row["wallet"])
        for row in discovery_engine.load_lifecycle(limit=100)
        if str(row.get("state")) in {"active", "watchlist"}
    ]
    registry = [row.wallet for row in list_traders(limit=200, db_path=traders_db_path)]

    random.seed(42)
    random_sample = random.sample(all_wallets, min(25, len(all_wallets))) if all_wallets else []

    overall_roi = _safe_float(copy.get("summary", {}).get("roi") if isinstance(copy.get("summary"), dict) else copy.get("roi"))
    if overall_roi == 0.0 and all_wallets:
        rois = [_safe_float(copy_by_wallet[w].get("roi")) for w in all_wallets if copy_by_wallet[w].get("roi") is not None]
        overall_roi = statistics.mean(rois) if rois else 0.0

    baselines = {
        "random_selection": _cohort_metrics(random_sample, copy_by_wallet, validations),
        "average_discovered": _cohort_metrics(discovered[:50], copy_by_wallet, validations),
        "average_promoted": _cohort_metrics(promoted[:50], copy_by_wallet, validations),
        "average_active": _cohort_metrics(active[:50], copy_by_wallet, validations),
        "paper_trading_baseline": {
            "wallet_count": len(all_wallets),
            "roi": round(overall_roi, 4),
            "expectancy": round(overall_roi, 4),
            "win_rate": None,
            "drawdown": None,
            "sharpe_like": None,
        },
    }

    promoted_roi = _safe_float(baselines["average_promoted"].get("roi"))
    discovered_roi = _safe_float(baselines["average_discovered"].get("roi"))
    avg_accuracy_values = [v.get("accuracy", 0) for v in validations.values() if v.get("validation_count", 0) >= 3]
    avg_accuracy = statistics.mean(avg_accuracy_values) if avg_accuracy_values else 0.0

    system_improving = False
    with closing_connection(traders_db_path) as conn:
        tables = {row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "wallet_alpha_lab_runs" in tables:
            rows = conn.execute(
                "SELECT summary_json FROM wallet_alpha_lab_runs ORDER BY id DESC LIMIT 5"
            ).fetchall()
            if len(rows) >= 2:
                import json

                latest = json.loads(rows[0]["summary_json"])
                prior = json.loads(rows[1]["summary_json"])
                latest_promo = latest.get("promotion_validation", {}).get("promoted", {}).get("avg_alpha", 0)
                prior_promo = prior.get("promotion_validation", {}).get("promoted", {}).get("avg_alpha", 0)
                system_improving = latest_promo >= prior_promo

    return _with_flags(
        {
            "baselines": baselines,
            "promoted_vs_average_roi_delta": round(promoted_roi - discovered_roi, 4) if promoted_roi and discovered_roi else None,
            "discovered_signal_useful": avg_accuracy > 0.5,
            "average_signal_accuracy": round(avg_accuracy, 4),
            "system_improving": system_improving,
            "registry_wallet_count": len(registry),
        }
    )
