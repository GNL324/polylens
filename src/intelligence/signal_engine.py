from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.analysis.trader_signal_engine import (
    DEFAULT_TRADER_SIGNAL_DB,
    generate_signals_from_activity,
    init_trader_signal_db,
    load_activity_json,
    persist_signals,
    run_trader_signal_cycle,
    score_trader_signals,
)
from src.analysis.trader_signal_paper_bridge import run_trader_signal_paper_bridge
from src.intelligence.strategy_classifier import StrategyClassifier
from src.intelligence.wallet_tracker import WalletTracker
from src.sqlite_utils import closing_connection

DEFAULT_MAX_SIGNAL_AGE_SECONDS = 86_400.0
DEFAULT_MIN_LIQUIDITY_AMOUNT = 1.0
DEFAULT_MIN_LIQUIDITY_SHARES = 1.0


@dataclass(frozen=True)
class SignalEngineConfig:
    max_signal_age_seconds: float = DEFAULT_MAX_SIGNAL_AGE_SECONDS
    min_liquidity_amount: float = DEFAULT_MIN_LIQUIDITY_AMOUNT
    min_liquidity_shares: float = DEFAULT_MIN_LIQUIDITY_SHARES
    recommendation_limit: int = 20
    paper_bridge_limit: int = 20


def _with_flags(payload: dict[str, Any]) -> dict[str, Any]:
    return {"read_only": True, "paper_only": True, **payload}


def _position_key(wallet: str, market_id: str, side: str) -> str:
    return f"{wallet}:{market_id}:{side}".lower()


class SignalEngine:
    """Monitor watched wallets and generate wallet-derived signals through existing pipelines."""

    def __init__(
        self,
        *,
        wallet_tracker: WalletTracker | None = None,
        strategy_classifier: StrategyClassifier | None = None,
        signal_db_path: str | Path = DEFAULT_TRADER_SIGNAL_DB,
        config: SignalEngineConfig | None = None,
    ) -> None:
        self.wallet_tracker = wallet_tracker or WalletTracker()
        self.strategy_classifier = strategy_classifier or StrategyClassifier(
            traders_db_path=self.wallet_tracker.traders_db_path,
        )
        self.signal_db_path = Path(signal_db_path)
        self.config = config or SignalEngineConfig()

    def _existing_position_keys(self) -> set[str]:
        init_trader_signal_db(self.signal_db_path)
        keys: set[str] = set()
        with closing_connection(self.signal_db_path) as conn:
            rows = conn.execute(
                """
                SELECT wallet, market_id, side
                FROM trader_signals
                WHERE action = 'buy'
                """
            ).fetchall()
        for row in rows:
            keys.add(_position_key(str(row["wallet"]), str(row["market_id"]), str(row["side"])))
        return keys

    def _is_stale(self, timestamp: float, *, now: float | None = None) -> bool:
        reference = now if now is not None else time.time()
        age = reference - float(timestamp or 0.0)
        return age > self.config.max_signal_age_seconds

    def _has_liquidity(self, signal: dict[str, Any]) -> bool:
        amount = float(signal.get("amount") or 0.0)
        shares = float(signal.get("shares") or 0.0)
        if amount >= self.config.min_liquidity_amount:
            return True
        if shares >= self.config.min_liquidity_shares:
            return True
        return False

    def filter_signals(self, signals: list[dict[str, Any]], *, now: float | None = None) -> tuple[list[dict[str, Any]], dict[str, int]]:
        existing = self._existing_position_keys()
        accepted: list[dict[str, Any]] = []
        rejected = {"stale": 0, "illiquid": 0, "duplicate": 0}
        seen_keys: set[str] = set()

        for signal in signals:
            timestamp = float(signal.get("timestamp") or 0.0)
            if self._is_stale(timestamp, now=now):
                rejected["stale"] += 1
                continue
            if not self._has_liquidity(signal):
                rejected["illiquid"] += 1
                continue
            position_key = _position_key(
                str(signal.get("wallet") or ""),
                str(signal.get("market_id") or ""),
                str(signal.get("side") or ""),
            )
            if position_key in existing or position_key in seen_keys:
                rejected["duplicate"] += 1
                continue
            seen_keys.add(position_key)
            accepted.append(signal)

        return accepted, rejected

    def load_events_for_wallets(self, wallets: list[str] | None = None) -> list[dict[str, Any]]:
        paths = self.wallet_tracker.activity_export_paths(wallets)
        events: list[dict[str, Any]] = []
        for path in paths:
            events.extend(load_activity_json(path))
        return events

    def generate_wallet_signals(
        self,
        events: list[dict[str, Any]] | None = None,
        *,
        persist: bool = True,
    ) -> dict[str, Any]:
        events = events if events is not None else self.load_events_for_wallets()
        raw_signals = generate_signals_from_activity(events)
        filtered, rejected = self.filter_signals(raw_signals)
        result: dict[str, Any] = {
            "events_loaded": len(events),
            "signals_generated": len(raw_signals),
            "signals_accepted": len(filtered),
            "signals_rejected": rejected,
            "signals": filtered,
        }
        if persist and filtered:
            persisted = persist_signals(filtered, db_path=self.signal_db_path)
            scored = score_trader_signals(db_path=self.signal_db_path)
            result["persisted"] = persisted
            result["scored"] = scored
        return _with_flags(result)

    def run_signal_cycle(
        self,
        *,
        activity_paths: list[str | Path] | None = None,
        outcomes_path: str | Path | None = None,
    ) -> dict[str, Any]:
        paths = [Path(path) for path in activity_paths] if activity_paths else self.wallet_tracker.activity_export_paths()
        events: list[dict[str, Any]] = []
        for path in paths:
            events.extend(load_activity_json(path))
        generation = self.generate_wallet_signals(events, persist=True)
        recommendations_cycle = run_trader_signal_cycle(
            activity_path=None,
            outcomes_path=outcomes_path,
            db_path=self.signal_db_path,
            recommendation_limit=self.config.recommendation_limit,
        )
        return _with_flags(
            {
                "export_paths": [str(path) for path in paths],
                "generation": generation,
                "recommendations": recommendations_cycle.get("recommendations"),
            }
        )

    def integrate_paper_trading(self) -> dict[str, Any]:
        from src.analysis.trader_signal_paper_bridge import PaperBridgeConfig

        bridge = run_trader_signal_paper_bridge(
            db_path=self.signal_db_path,
            config=PaperBridgeConfig(limit=self.config.paper_bridge_limit),
        )
        return _with_flags({"paper_bridge": bridge})

    def process_watched_wallets(
        self,
        *,
        refresh: bool = True,
        classify: bool = True,
        outcomes_path: str | Path | None = None,
    ) -> dict[str, Any]:
        watchlist = self.wallet_tracker.load_watchlist()
        wallets = [entry.wallet for entry in watchlist]

        refresh_result: list[dict[str, Any]] = []
        if refresh:
            refresh_result = self.wallet_tracker.refresh_wallets(wallets)

        profiles = []
        if classify:
            profiles = self.strategy_classifier.classify_wallets(
                wallets,
                activity_dir=self.wallet_tracker.wallet_export_dir,
            )
            self.strategy_classifier.persist_profiles(profiles)

        signal_cycle = self.run_signal_cycle(outcomes_path=outcomes_path)
        paper_bridge = self.integrate_paper_trading()

        return _with_flags(
            {
                "wallets_processed": len(wallets),
                "refresh": refresh_result,
                "strategy_profiles": [profile.to_dict() for profile in profiles],
                "signal_cycle": signal_cycle,
                "paper_bridge": paper_bridge,
            }
        )


def run_wallet_intelligence_cycle(
    *,
    wallet_tracker: WalletTracker | None = None,
    strategy_classifier: StrategyClassifier | None = None,
    signal_engine: SignalEngine | None = None,
    watchlist_limit: int = 50,
    outcomes_path: str | Path | None = None,
) -> dict[str, Any]:
    """End-to-end wallet intelligence cycle without a parallel orchestration framework."""
    tracker = wallet_tracker or WalletTracker()
    classifier = strategy_classifier or StrategyClassifier(traders_db_path=tracker.traders_db_path)
    engine = signal_engine or SignalEngine(wallet_tracker=tracker, strategy_classifier=classifier)

    watchlist = tracker.build_ranked_watchlist(limit=watchlist_limit)
    watchlist_result = tracker.persist_watchlist(watchlist)
    processing = engine.process_watched_wallets(outcomes_path=outcomes_path)

    return _with_flags(
        {
            "watchlist": watchlist_result,
            "processing": processing,
        }
    )
