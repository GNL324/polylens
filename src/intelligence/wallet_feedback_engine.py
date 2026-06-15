from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from src.analysis.trader_discovery import DEFAULT_TRADER_DISCOVERY_DB
from src.analysis.trader_registry import DEFAULT_TRADERS_DB, list_traders
from src.intelligence.wallet_discovery import LIFECYCLE_STATES, WalletDiscoveryEngine, init_wallet_discovery_db
from src.intelligence.wallet_performance import (
    PERFORMANCE_STATUSES,
    WalletPerformanceEngine,
    WalletPerformanceScore,
    init_wallet_performance_db,
)
from src.sqlite_utils import closing_connection

PERFORMANCE_TO_LIFECYCLE = {
    "promoted": "active",
    "active": "watchlist",
    "probation": "probation",
    "retired": "retired",
}


@dataclass
class FeedbackConfig:
    promote_score_threshold: float = 75.0
    demote_score_threshold: float = 30.0
    retire_score_threshold: float = 20.0
    reactivate_score_threshold: float = 55.0
    min_confidence_for_promote: float = 0.5
    min_win_rate_for_promote: float = 0.55
    allow_reactivation: bool = True


class WalletFeedbackEngine:
    """Continuously evaluate wallets and apply performance-driven lifecycle changes."""

    def __init__(
        self,
        *,
        config: FeedbackConfig | None = None,
        traders_db_path: str = DEFAULT_TRADERS_DB,
        discovery_db_path: str = DEFAULT_TRADER_DISCOVERY_DB,
        performance_engine: WalletPerformanceEngine | None = None,
        discovery_engine: WalletDiscoveryEngine | None = None,
    ) -> None:
        self.config = config or FeedbackConfig()
        self.traders_db_path = traders_db_path
        self.discovery_db_path = discovery_db_path
        self._performance = performance_engine or WalletPerformanceEngine(
            traders_db_path=traders_db_path,
            discovery_db_path=discovery_db_path,
        )
        self._discovery = discovery_engine or WalletDiscoveryEngine(
            traders_db_path=traders_db_path,
            discovery_db_path=discovery_db_path,
        )
        init_wallet_performance_db(traders_db_path)
        init_wallet_discovery_db(traders_db_path, discovery_db_path)

    def _load_wallets(self, wallets: list[str] | None = None) -> list[str]:
        if wallets:
            return [w.lower() for w in wallets if w]
        discovered = [row.wallet for row in self._discovery.load_recent_discoveries(limit=200)]
        registry = [row.wallet for row in list_traders(limit=200, db_path=str(self.traders_db_path))]
        seen: set[str] = set()
        ordered: list[str] = []
        for wallet in discovered + registry:
            normalized = str(wallet or "").strip().lower()
            if normalized and normalized not in seen:
                seen.add(normalized)
                ordered.append(normalized)
        return ordered

    def _current_lifecycle_state(self, wallet: str) -> str:
        rows = self._discovery.load_lifecycle(limit=500)
        for row in rows:
            if row.get("wallet") == wallet:
                return str(row.get("state") or "probation")
        return "probation"

    def _evaluate_status(self, score: WalletPerformanceScore) -> str:
        cfg = self.config
        win_rate = float(score.metrics.get("win_rate") or 0.0)
        if (
            score.score >= cfg.promote_score_threshold
            and score.confidence >= cfg.min_confidence_for_promote
            and win_rate >= cfg.min_win_rate_for_promote
        ):
            return "promoted"
        if score.score < cfg.retire_score_threshold and score.trend == "declining":
            return "retired"
        if score.score < cfg.demote_score_threshold:
            return "probation"
        if score.status == "retired" and cfg.allow_reactivation and score.score >= cfg.reactivate_score_threshold:
            return "active"
        if score.score >= cfg.reactivate_score_threshold:
            return "active"
        return score.status if score.status in PERFORMANCE_STATUSES else "active"

    def _action_for_transition(self, previous: str, new_status: str) -> str | None:
        previous_lifecycle = PERFORMANCE_TO_LIFECYCLE.get(previous, previous)
        new_lifecycle = PERFORMANCE_TO_LIFECYCLE.get(new_status, new_status)
        if new_status == "promoted" and previous != "promoted":
            return "promoted"
        if new_status == "retired" and previous != "retired":
            return "retired"
        if new_lifecycle != previous_lifecycle:
            prev_idx = LIFECYCLE_STATES.index(previous_lifecycle) if previous_lifecycle in LIFECYCLE_STATES else 2
            new_idx = LIFECYCLE_STATES.index(new_lifecycle) if new_lifecycle in LIFECYCLE_STATES else 2
            if new_idx < prev_idx:
                return "promoted"
            if new_idx > prev_idx:
                return "demoted"
        if previous == "retired" and new_status in {"active", "promoted"}:
            return "reactivated"
        return None

    def evaluate_wallet(self, wallet: str) -> dict[str, Any]:
        performance = self._performance.score_wallet(wallet)
        previous_status = self._current_lifecycle_state(wallet)
        previous_perf = performance.status
        new_status = self._evaluate_status(performance)
        performance.status = new_status
        self._performance.persist_snapshot(performance)

        action = self._action_for_transition(previous_perf, new_status)
        lifecycle_state = PERFORMANCE_TO_LIFECYCLE.get(new_status, "probation")
        reason = (
            f"performance_score={performance.score:.2f} trend={performance.trend} "
            f"confidence={performance.confidence:.2f}"
        )

        if action:
            self._performance.log_action(
                wallet=wallet,
                action=action,
                previous_status=previous_perf,
                new_status=new_status,
                score=performance.score,
                reason=reason,
            )
            self._update_lifecycle(wallet, lifecycle_state, performance, reason)

        return {
            "wallet": wallet,
            "score": performance.score,
            "confidence": performance.confidence,
            "trend": performance.trend,
            "previous_status": previous_perf,
            "new_status": new_status,
            "lifecycle_state": lifecycle_state,
            "action": action,
            "reason": reason,
        }

    def _update_lifecycle(
        self,
        wallet: str,
        lifecycle_state: str,
        performance: WalletPerformanceScore,
        reason: str,
    ) -> None:
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        with closing_connection(self.traders_db_path) as conn:
            existing = conn.execute(
                "SELECT state FROM wallet_lifecycle WHERE wallet = ?",
                (wallet,),
            ).fetchone()
            previous_state = str(existing["state"]) if existing else "probation"
            if existing is None:
                conn.execute(
                    """
                    INSERT INTO wallet_lifecycle (
                        wallet, state, score, confidence, category, rank,
                        last_scored_at, last_active_at, stale_days, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        wallet,
                        lifecycle_state,
                        performance.score,
                        performance.confidence,
                        performance.status,
                        0,
                        now,
                        None,
                        0,
                        now,
                    ),
                )
            else:
                conn.execute(
                    """
                    UPDATE wallet_lifecycle
                    SET state = ?, score = ?, confidence = ?, category = ?, updated_at = ?, last_scored_at = ?
                    WHERE wallet = ?
                    """,
                    (
                        lifecycle_state,
                        performance.score,
                        performance.confidence,
                        performance.status,
                        now,
                        now,
                        wallet,
                    ),
                )
            if previous_state != lifecycle_state:
                conn.execute(
                    """
                    INSERT INTO wallet_lifecycle_events (
                        wallet, previous_state, new_state, reason, score, recorded_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (wallet, previous_state, lifecycle_state, f"performance_feedback: {reason}", performance.score, now),
                )

    def evaluate_all(self, wallets: list[str] | None = None) -> dict[str, Any]:
        targets = self._load_wallets(wallets)
        results = [self.evaluate_wallet(wallet) for wallet in targets]
        actions = [row for row in results if row.get("action")]
        return {
            "read_only": True,
            "paper_only": True,
            "evaluated": len(results),
            "actions_taken": len(actions),
            "promotions": sum(1 for row in actions if row.get("action") == "promoted"),
            "demotions": sum(1 for row in actions if row.get("action") == "demoted"),
            "retirements": sum(1 for row in actions if row.get("action") == "retired"),
            "reactivations": sum(1 for row in actions if row.get("action") == "reactivated"),
            "results": results,
        }

    def run_feedback_cycle(self, wallets: list[str] | None = None) -> dict[str, Any]:
        evaluation = self.evaluate_all(wallets)
        feedback = self._discovery.apply_performance_feedback()
        return {**evaluation, "discovery_feedback": feedback}


def run_wallet_feedback_cycle(
    *,
    traders_db_path: str = DEFAULT_TRADERS_DB,
    discovery_db_path: str = DEFAULT_TRADER_DISCOVERY_DB,
    wallets: list[str] | None = None,
    config: FeedbackConfig | None = None,
) -> dict[str, Any]:
    engine = WalletFeedbackEngine(
        config=config,
        traders_db_path=traders_db_path,
        discovery_db_path=discovery_db_path,
    )
    return engine.run_feedback_cycle(wallets)
