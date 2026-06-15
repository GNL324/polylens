from __future__ import annotations

from pathlib import Path
from typing import Any

from src.analysis.paper_copy_trader import DEFAULT_PAPER_COPY_DB, run_paper_copy_trader
from src.analysis.trader_signal_engine import DEFAULT_TRADER_SIGNAL_DB
from src.intelligence.signal_engine import SignalEngine, run_wallet_intelligence_cycle
from src.intelligence.strategy_classifier import StrategyClassifier, StrategyProfile
from src.intelligence.wallet_signal_analytics import wallet_signal_analytics_report
from src.intelligence.wallet_tracker import WalletTracker
from src.sqlite_utils import closing_connection

DEFAULT_SYNC_LIMIT = 20


def _with_flags(payload: dict[str, Any]) -> dict[str, Any]:
    return {"read_only": True, "paper_only": True, **payload}


def link_profile_to_registry(profile: StrategyProfile, *, traders_db_path: str | Path) -> None:
    """Annotate the latest wallet report with strategy archetype metadata."""
    import json

    from src.analysis.trader_registry import load_wallet_report

    record = load_wallet_report(profile.wallet, db_path=str(traders_db_path))
    if record is None:
        return
    payload = record.to_dict()
    report = payload.get("report") or {}
    report["strategy_archetype"] = profile.archetype
    report["strategy_confidence"] = profile.confidence
    report["strategy_specialization"] = profile.specialization
    with closing_connection(traders_db_path) as conn:
        conn.execute(
            """
            UPDATE wallet_reports
            SET report_json = ?
            WHERE wallet = ? AND report_timestamp = ?
            """,
            (
                json.dumps(report, sort_keys=True),
                profile.wallet,
                record.report_timestamp,
            ),
        )


def sync_profiles_to_registry(
    profiles: list[StrategyProfile],
    *,
    traders_db_path: str | Path,
) -> dict[str, Any]:
    linked = 0
    for profile in profiles:
        try:
            link_profile_to_registry(profile, traders_db_path=traders_db_path)
            linked += 1
        except Exception:
            continue
    return {"profiles_linked": linked, "profiles_total": len(profiles)}


def emit_wallet_recommendations_to_paper_trading(
    *,
    wallet_tracker: WalletTracker,
    signal_engine: SignalEngine,
    sync_limit: int = DEFAULT_SYNC_LIMIT,
    run_copy_trader: bool = True,
) -> dict[str, Any]:
    """Push wallet signals into existing paper-trading paths (bridge + copy trader)."""
    sync = wallet_tracker.sync_watchlist_to_paper_copy(limit=sync_limit)
    bridge = signal_engine.integrate_paper_trading()
    copy_result: dict[str, Any] | None = None
    if run_copy_trader:
        copy_result = run_paper_copy_trader(
            db_path=wallet_tracker.paper_copy_db_path,
            wallets=sync.get("wallets"),
        )
    return _with_flags(
        {
            "watchlist_sync": sync,
            "paper_bridge": bridge,
            "paper_copy_trader": copy_result,
        }
    )


def run_wallet_signal_integration_cycle(
    *,
    wallet_tracker: WalletTracker | None = None,
    strategy_classifier: StrategyClassifier | None = None,
    signal_engine: SignalEngine | None = None,
    watchlist_limit: int = 50,
    sync_limit: int = DEFAULT_SYNC_LIMIT,
    outcomes_path: str | Path | None = None,
    run_copy_trader: bool = True,
) -> dict[str, Any]:
    """Full wallet signal integration: intelligence cycle → registry → paper trading → analytics."""
    tracker = wallet_tracker or WalletTracker()
    classifier = strategy_classifier or StrategyClassifier(traders_db_path=tracker.traders_db_path)
    engine = signal_engine or SignalEngine(
        wallet_tracker=tracker,
        strategy_classifier=classifier,
        signal_db_path=DEFAULT_TRADER_SIGNAL_DB,
    )

    intelligence = run_wallet_intelligence_cycle(
        wallet_tracker=tracker,
        strategy_classifier=classifier,
        signal_engine=engine,
        watchlist_limit=watchlist_limit,
        outcomes_path=outcomes_path,
    )

    profiles = [
        StrategyProfile(**row)
        for row in intelligence.get("processing", {}).get("strategy_profiles", [])
        if isinstance(row, dict)
    ]
    registry_link = sync_profiles_to_registry(profiles, traders_db_path=tracker.traders_db_path)
    registry_sync = intelligence.get("processing", {}).get("registry_sync") or tracker.sync_watchlist_to_registry()
    paper_trading = intelligence.get("processing", {}).get("paper_trading")
    if paper_trading is None:
        paper_trading = emit_wallet_recommendations_to_paper_trading(
            wallet_tracker=tracker,
            signal_engine=engine,
            sync_limit=sync_limit,
            run_copy_trader=run_copy_trader,
        )

    analytics = wallet_signal_analytics_report(
        paper_copy_db_path=tracker.paper_copy_db_path,
        traders_db_path=tracker.traders_db_path,
        signal_db_path=engine.signal_db_path,
    )

    return _with_flags(
        {
            "intelligence": intelligence,
            "registry_link": registry_link,
            "registry_sync": registry_sync,
            "paper_trading": paper_trading,
            "analytics": analytics,
        }
    )
