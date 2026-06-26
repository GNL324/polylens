from __future__ import annotations

from pathlib import Path
from typing import Any

from src.analysis.paper_copy_trader import DEFAULT_PAPER_COPY_DB, paper_copy_report
from src.analysis.trader_signal_engine import DEFAULT_TRADER_SIGNAL_DB, init_trader_signal_db
from src.analysis.trader_signal_paper_bridge import init_trader_signal_paper_bridge_db
from src.analysis.trader_signal_validation import init_trader_signal_validation_db
from src.intelligence.strategy_classifier import init_strategy_profiles_db
from src import sqlite_utils

DEFAULT_TRADERS_DB = "data/traders.db"
_REPO_ROOT = Path(__file__).resolve().parents[2]
_PRODUCTION_TRADER_SIGNAL_DB = (_REPO_ROOT / DEFAULT_TRADER_SIGNAL_DB).resolve()


def _with_flags(payload: dict[str, Any]) -> dict[str, Any]:
    return {"read_only": True, "paper_only": True, **payload}


def _safe_float(value: Any, default: float = 0.0) -> float:
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _load_strategy_profiles(traders_db_path: str | Path) -> dict[str, dict[str, Any]]:
    path = Path(traders_db_path)
    if not path.exists():
        return {}
    init_strategy_profiles_db(path)
    with sqlite_utils.closing_connection(path) as conn:
        rows = conn.execute(
            "SELECT wallet, archetype, confidence, alpha_score, watch_score FROM strategy_profiles"
        ).fetchall()
    return {
        str(row["wallet"]): {
            "archetype": str(row["archetype"]),
            "confidence": float(row["confidence"] or 0.0),
            "alpha_score": int(row["alpha_score"] or 0),
            "watch_score": int(row["watch_score"] or 0),
        }
        for row in rows
    }


def _wallet_validation_stats(signal_db_path: str | Path) -> dict[str, dict[str, Any]]:
    path = Path(signal_db_path)
    if not path.exists() and not _is_default_trader_signal_db(path):
        return {}
    init_trader_signal_db(path)
    init_trader_signal_validation_db(path)
    with sqlite_utils.closing_connection(path) as conn:
        rows = conn.execute(
            """
            SELECT
                trader_address,
                COUNT(*) AS validation_count,
                ROUND(AVG(correct), 6) AS accuracy,
                ROUND(AVG(roi_proxy), 6) AS avg_roi_proxy
            FROM trader_signal_validation
            GROUP BY trader_address
            """
        ).fetchall()
    return {
        str(row["trader_address"]): {
            "validation_count": int(row["validation_count"] or 0),
            "accuracy": _safe_float(row["accuracy"]),
            "avg_roi_proxy": _safe_float(row["avg_roi_proxy"]),
        }
        for row in rows
    }


def _is_default_trader_signal_db(path: Path) -> bool:
    try:
        return path.resolve() == _PRODUCTION_TRADER_SIGNAL_DB
    except OSError:
        return False


def wallet_performance_rows(
    *,
    paper_copy_db_path: str | Path = DEFAULT_PAPER_COPY_DB,
    traders_db_path: str | Path = DEFAULT_TRADERS_DB,
    signal_db_path: str | Path = DEFAULT_TRADER_SIGNAL_DB,
    limit: int = 25,
) -> list[dict[str, Any]]:
    copy = paper_copy_report(db_path=paper_copy_db_path)
    profiles = _load_strategy_profiles(traders_db_path)
    validations = _wallet_validation_stats(signal_db_path)
    rows: list[dict[str, Any]] = []

    for wallet, stats in copy.get("by_wallet", {}).items():
        closed = int(stats.get("closed_positions") or 0)
        wins = 0
        if closed > 0:
            path = Path(paper_copy_db_path)
            if path.exists():
                with sqlite_utils.closing_connection(path) as conn:
                    wins = int(
                        conn.execute(
                            """
                            SELECT COUNT(*) FROM paper_copy_positions
                            WHERE source_wallet = ? AND status = 'closed' AND COALESCE(pnl, 0) > 0
                            """,
                            (wallet,),
                        ).fetchone()[0]
                    )
        profile = profiles.get(wallet, {})
        validation = validations.get(wallet, {})
        rows.append(
            {
                "wallet": wallet,
                "archetype": profile.get("archetype", "unknown"),
                "copied_trades": int(stats.get("copied_trades") or 0),
                "closed_positions": closed,
                "open_positions": int(stats.get("open_positions") or 0),
                "win_rate": round(wins / closed, 6) if closed else None,
                "roi": _safe_float(stats.get("roi")),
                "realized_pnl": _safe_float(stats.get("realized_pnl")),
                "signal_accuracy": validation.get("accuracy"),
                "signal_validation_count": validation.get("validation_count", 0),
                "alpha_score": profile.get("alpha_score", 0),
                "watch_score": profile.get("watch_score", 0),
            }
        )

    rows.sort(
        key=lambda row: (
            -(row["win_rate"] or 0.0),
            -(row["roi"] or 0.0),
            -(row["realized_pnl"] or 0.0),
            row["wallet"],
        )
    )
    return rows[: max(int(limit), 0)]


def top_copied_wallets_report(
    *,
    paper_copy_db_path: str | Path = DEFAULT_PAPER_COPY_DB,
    traders_db_path: str | Path = DEFAULT_TRADERS_DB,
    signal_db_path: str | Path = DEFAULT_TRADER_SIGNAL_DB,
    limit: int = 15,
) -> dict[str, Any]:
    wallets = wallet_performance_rows(
        paper_copy_db_path=paper_copy_db_path,
        traders_db_path=traders_db_path,
        signal_db_path=signal_db_path,
        limit=limit,
    )
    return _with_flags(
        {
            "count": len(wallets),
            "wallets": wallets,
        }
    )


def archetype_performance_report(
    *,
    paper_copy_db_path: str | Path = DEFAULT_PAPER_COPY_DB,
    traders_db_path: str | Path = DEFAULT_TRADERS_DB,
    signal_db_path: str | Path = DEFAULT_TRADER_SIGNAL_DB,
) -> dict[str, Any]:
    rows = wallet_performance_rows(
        paper_copy_db_path=paper_copy_db_path,
        traders_db_path=traders_db_path,
        signal_db_path=signal_db_path,
        limit=10_000,
    )
    buckets: dict[str, dict[str, Any]] = {}
    for row in rows:
        archetype = str(row.get("archetype") or "unknown")
        bucket = buckets.setdefault(
            archetype,
            {
                "archetype": archetype,
                "wallet_count": 0,
                "closed_positions": 0,
                "wins": 0,
                "realized_pnl": 0.0,
                "roi_total": 0.0,
                "roi_count": 0,
            },
        )
        bucket["wallet_count"] += 1
        closed = int(row.get("closed_positions") or 0)
        bucket["closed_positions"] += closed
        if row.get("win_rate") is not None and closed:
            bucket["wins"] += int(round(float(row["win_rate"]) * closed))
        bucket["realized_pnl"] = round(bucket["realized_pnl"] + _safe_float(row.get("realized_pnl")), 6)
        if row.get("roi") is not None:
            bucket["roi_total"] += float(row["roi"])
            bucket["roi_count"] += 1

    archetypes = []
    for bucket in buckets.values():
        closed = int(bucket["closed_positions"] or 0)
        archetypes.append(
            {
                "archetype": bucket["archetype"],
                "wallet_count": bucket["wallet_count"],
                "closed_positions": closed,
                "win_rate": round(bucket["wins"] / closed, 6) if closed else None,
                "avg_roi": round(bucket["roi_total"] / bucket["roi_count"], 6) if bucket["roi_count"] else None,
                "realized_pnl": bucket["realized_pnl"],
            }
        )
    archetypes.sort(key=lambda row: (-(row["realized_pnl"] or 0.0), row["archetype"]))
    return _with_flags({"archetypes": archetypes, "count": len(archetypes)})


def wallet_signal_analytics_report(
    *,
    paper_copy_db_path: str | Path = DEFAULT_PAPER_COPY_DB,
    traders_db_path: str | Path = DEFAULT_TRADERS_DB,
    signal_db_path: str | Path = DEFAULT_TRADER_SIGNAL_DB,
    wallet_limit: int = 25,
    top_copied_limit: int = 15,
) -> dict[str, Any]:
    copy = paper_copy_report(db_path=paper_copy_db_path)
    wallets = wallet_performance_rows(
        paper_copy_db_path=paper_copy_db_path,
        traders_db_path=traders_db_path,
        signal_db_path=signal_db_path,
        limit=wallet_limit,
    )
    top_copied = top_copied_wallets_report(
        paper_copy_db_path=paper_copy_db_path,
        traders_db_path=traders_db_path,
        signal_db_path=signal_db_path,
        limit=top_copied_limit,
    )
    archetypes = archetype_performance_report(
        paper_copy_db_path=paper_copy_db_path,
        traders_db_path=traders_db_path,
        signal_db_path=signal_db_path,
    )

    signal_summary: dict[str, Any] = {}
    signal_path = Path(signal_db_path)
    if signal_path.exists():
        init_trader_signal_db(signal_path)
        init_trader_signal_paper_bridge_db(signal_path)
        with sqlite_utils.closing_connection(signal_path) as conn:
            overview = conn.execute(
                """
                SELECT
                    (SELECT COUNT(*) FROM trader_signals) AS total_signals,
                    (SELECT COUNT(*) FROM trader_signal_recommendations) AS total_recommendations,
                    (SELECT COUNT(*) FROM trader_signal_paper_intents WHERE status='simulated') AS simulated_intents
                """
            ).fetchone()
        if overview:
            signal_summary = {
                "total_signals": int(overview["total_signals"] or 0),
                "total_recommendations": int(overview["total_recommendations"] or 0),
                "simulated_intents": int(overview["simulated_intents"] or 0),
            }

    return _with_flags(
        {
            "summary": {
                **signal_summary,
                "copied_trades": int(copy.get("copied_trades") or 0),
                "closed_positions": int(copy.get("closed_positions") or 0),
                "portfolio_win_rate": copy.get("win_rate"),
                "portfolio_roi": copy.get("roi"),
                "realized_pnl": copy.get("realized_pnl"),
            },
            "wallets": wallets,
            "top_copied_wallets": top_copied["wallets"],
            "archetype_performance": archetypes["archetypes"],
        }
    )
