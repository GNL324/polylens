from __future__ import annotations

import json
import math
import statistics
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.analysis.paper_copy_trader import DEFAULT_PAPER_COPY_DB
from src.analysis.trader_discovery import DEFAULT_TRADER_DISCOVERY_DB
from src.analysis.trader_registry import DEFAULT_TRADERS_DB
from src.analysis.trader_signal_engine import DEFAULT_TRADER_SIGNAL_DB
from src.intelligence.wallet_discovery import init_wallet_discovery_db
from src.intelligence.wallet_scoring import WalletScorer
from src.intelligence.wallet_signal_analytics import _wallet_validation_stats
from src.intelligence.wallet_synthetic_filter import is_synthetic_wallet
from src.sqlite_utils import closing_connection

PERFORMANCE_STATUSES = ("promoted", "active", "probation", "retired")
PERFORMANCE_ACTIONS = ("promoted", "demoted", "retired", "reactivated", "snapshot")


@dataclass
class WalletPerformanceScore:
    wallet: str
    score: float
    confidence: float
    trend: str
    status: str
    metrics: dict[str, Any] = field(default_factory=dict)
    components: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _safe_float(value: Any, default: float = 0.0) -> float:
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def init_wallet_performance_db(traders_db_path: str | Path = DEFAULT_TRADERS_DB) -> None:
    init_wallet_discovery_db(traders_db_path)
    with closing_connection(traders_db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS wallet_performance_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                wallet TEXT NOT NULL,
                score REAL NOT NULL,
                confidence REAL NOT NULL,
                trend TEXT NOT NULL,
                status TEXT NOT NULL,
                metrics_json TEXT NOT NULL,
                components_json TEXT NOT NULL,
                recorded_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS wallet_performance_actions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                wallet TEXT NOT NULL,
                action TEXT NOT NULL,
                previous_status TEXT NOT NULL,
                new_status TEXT NOT NULL,
                score REAL NOT NULL,
                reason TEXT NOT NULL,
                recorded_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS wallet_performance_trends (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                wallet TEXT NOT NULL,
                score_delta REAL NOT NULL,
                previous_score REAL,
                current_score REAL NOT NULL,
                trend TEXT NOT NULL,
                recorded_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_wallet_performance_snapshots_wallet ON wallet_performance_snapshots(wallet, recorded_at DESC);
            CREATE INDEX IF NOT EXISTS idx_wallet_performance_actions_wallet ON wallet_performance_actions(wallet, recorded_at DESC);
            CREATE INDEX IF NOT EXISTS idx_wallet_performance_actions_action ON wallet_performance_actions(action);
            CREATE INDEX IF NOT EXISTS idx_wallet_performance_trends_wallet ON wallet_performance_trends(wallet, recorded_at DESC);
            """
        )


class WalletPerformanceEngine:
    """Measure wallet performance from paper outcomes and signal validation data."""

    def __init__(
        self,
        *,
        traders_db_path: str | Path = DEFAULT_TRADERS_DB,
        discovery_db_path: str | Path = DEFAULT_TRADER_DISCOVERY_DB,
        signal_db_path: str | Path = DEFAULT_TRADER_SIGNAL_DB,
        paper_copy_db_path: str | Path = DEFAULT_PAPER_COPY_DB,
        wallet_activity_db_path: str | Path = "data/wallet_activity.db",
    ) -> None:
        self.traders_db_path = Path(traders_db_path)
        self.discovery_db_path = Path(discovery_db_path)
        self.signal_db_path = Path(signal_db_path)
        self.paper_copy_db_path = Path(paper_copy_db_path)
        self.wallet_activity_db_path = Path(wallet_activity_db_path)
        self._scorer = WalletScorer(
            traders_db_path=traders_db_path,
            discovery_db_path=discovery_db_path,
            signal_db_path=signal_db_path,
            paper_copy_db_path=paper_copy_db_path,
            wallet_activity_db_path=wallet_activity_db_path,
        )
        init_wallet_performance_db(self.traders_db_path)

    def _closed_trade_rois(self, wallet: str) -> list[float]:
        path = self.paper_copy_db_path
        if not path.exists():
            return []
        with closing_connection(path) as conn:
            rows = conn.execute(
                """
                SELECT roi FROM paper_copy_positions
                WHERE source_wallet = ? AND status = 'closed' AND roi IS NOT NULL
                """,
                (wallet.lower(),),
            ).fetchall()
        return [float(row["roi"]) for row in rows]

    def _signal_count(self, wallet: str) -> int:
        path = self.signal_db_path
        if not path.exists():
            return 0
        with closing_connection(path) as conn:
            tables = {row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "trader_signals" not in tables:
                return 0
            row = conn.execute(
                "SELECT COUNT(*) AS count FROM trader_signals WHERE wallet = ?",
                (wallet.lower(),),
            ).fetchone()
        return int(row["count"] or 0) if row else 0

    def _previous_snapshot_score(self, wallet: str) -> float | None:
        with closing_connection(self.traders_db_path) as conn:
            row = conn.execute(
                """
                SELECT score FROM wallet_performance_snapshots
                WHERE wallet = ?
                ORDER BY recorded_at DESC, id DESC
                LIMIT 1
                """,
                (wallet.lower(),),
            ).fetchone()
        return float(row["score"]) if row else None

    def _survival_days(self, wallet: str) -> int:
        with closing_connection(self.traders_db_path) as conn:
            row = conn.execute(
                "SELECT first_seen FROM wallets WHERE wallet = ?",
                (wallet.lower(),),
            ).fetchone()
            if row is None:
                return 0
            try:
                first = datetime.fromisoformat(str(row["first_seen"]).replace("Z", "+00:00"))
                return max(0, int((datetime.now(timezone.utc) - first).total_seconds() / 86_400))
            except ValueError:
                return 0

    def _compute_trend(self, current: float, previous: float | None) -> str:
        if previous is None:
            return "stable"
        delta = current - previous
        if delta >= 5:
            return "improving"
        if delta <= -5:
            return "declining"
        return "stable"

    def _status_from_score(self, score: float, *, win_rate: float, confidence: float, trend: str) -> str:
        if score >= 75 and win_rate >= 0.55 and confidence >= 0.5:
            return "promoted"
        if score >= 55:
            return "active"
        if score >= 30 or trend == "improving":
            return "probation"
        return "retired"

    def score_wallet(self, wallet: str) -> WalletPerformanceScore:
        wallet = str(wallet or "").strip().lower()
        if is_synthetic_wallet(wallet):
            return WalletPerformanceScore(
                wallet=wallet,
                score=0.0,
                confidence=0.0,
                status="retired",
                trend="stable",
                metrics={"synthetic": True},
                components={"synthetic_excluded": 100.0},
            )
        base = self._scorer.score_wallet(wallet)
        validations = _wallet_validation_stats(self.signal_db_path).get(wallet, {})
        rois = self._closed_trade_rois(wallet)
        win_rate = _safe_float(base.metrics.get("win_rate"))
        realized_roi = _safe_float(base.metrics.get("realized_roi"))
        signal_accuracy = _safe_float(validations.get("accuracy"))
        signal_roi_proxy = _safe_float(validations.get("avg_roi_proxy"))
        expectancy = statistics.mean(rois) if rois else signal_roi_proxy
        drawdown = abs(min(rois)) if rois else abs(min(0.0, realized_roi))
        sharpe_like = 0.0
        if len(rois) >= 2:
            std = statistics.pstdev(rois)
            sharpe_like = (statistics.mean(rois) / std) if std > 0 else 0.0
        signal_frequency = self._signal_count(wallet)
        signal_freshness = base.components.get("signal_freshness", 0.0)
        survival_days = self._survival_days(wallet)
        consistency = _clamp(signal_accuracy * 100.0) if validations.get("validation_count", 0) >= 5 else base.confidence * 50.0

        components = {
            "realized_roi": round(_clamp(realized_roi * 100 + 50) if realized_roi else base.components["roi"], 4),
            "expectancy": round(_clamp(expectancy * 100 + 50) if expectancy else base.components["expectancy"], 4),
            "win_rate": round(_clamp(win_rate * 100) if win_rate else base.components["win_rate"], 4),
            "drawdown": round(_clamp(100.0 - drawdown * 100), 4),
            "sharpe_like": round(_clamp(sharpe_like * 25 + 50), 4),
            "signal_frequency": round(_clamp(signal_frequency * 2), 4),
            "signal_freshness": round(signal_freshness, 4),
            "survival_duration": round(_clamp(survival_days), 4),
            "consistency": round(consistency, 4),
        }

        composite = round(
            _clamp(
                components["realized_roi"] * 0.20
                + components["expectancy"] * 0.12
                + components["win_rate"] * 0.15
                + components["drawdown"] * 0.08
                + components["sharpe_like"] * 0.10
                + components["signal_frequency"] * 0.05
                + components["signal_freshness"] * 0.10
                + components["survival_duration"] * 0.05
                + components["consistency"] * 0.15
            ),
            4,
        )

        previous = self._previous_snapshot_score(wallet)
        trend = self._compute_trend(composite, previous)
        status = self._status_from_score(composite, win_rate=win_rate or 0.0, confidence=base.confidence, trend=trend)

        return WalletPerformanceScore(
            wallet=wallet,
            score=composite,
            confidence=base.confidence,
            trend=trend,
            status=status,
            components=components,
            metrics={
                **base.metrics,
                "expectancy": round(expectancy, 4) if expectancy else None,
                "drawdown": round(drawdown, 4),
                "sharpe_like": round(sharpe_like, 4),
                "signal_frequency": signal_frequency,
                "survival_days": survival_days,
                "signal_accuracy": signal_accuracy or None,
                "previous_score": previous,
            },
        )

    def score_wallets(self, wallets: list[str]) -> list[WalletPerformanceScore]:
        return [self.score_wallet(wallet) for wallet in wallets]

    def persist_snapshot(self, performance: WalletPerformanceScore) -> None:
        now = _utc_now()
        previous = performance.metrics.get("previous_score")
        if previous is not None:
            delta = performance.score - float(previous)
            trend = performance.trend
            with closing_connection(self.traders_db_path) as conn:
                conn.execute(
                    """
                    INSERT INTO wallet_performance_trends (
                        wallet, score_delta, previous_score, current_score, trend, recorded_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (performance.wallet, delta, previous, performance.score, trend, now),
                )
        with closing_connection(self.traders_db_path) as conn:
            conn.execute(
                """
                INSERT INTO wallet_performance_snapshots (
                    wallet, score, confidence, trend, status, metrics_json, components_json, recorded_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    performance.wallet,
                    performance.score,
                    performance.confidence,
                    performance.trend,
                    performance.status,
                    json.dumps(performance.metrics, sort_keys=True),
                    json.dumps(performance.components, sort_keys=True),
                    now,
                ),
            )

    def log_action(
        self,
        *,
        wallet: str,
        action: str,
        previous_status: str,
        new_status: str,
        score: float,
        reason: str,
    ) -> None:
        with closing_connection(self.traders_db_path) as conn:
            conn.execute(
                """
                INSERT INTO wallet_performance_actions (
                    wallet, action, previous_status, new_status, score, reason, recorded_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (wallet, action, previous_status, new_status, score, reason, _utc_now()),
            )

    def load_snapshots(self, wallet: str | None = None, *, limit: int = 50) -> list[dict[str, Any]]:
        query = """
            SELECT wallet, score, confidence, trend, status, metrics_json, components_json, recorded_at
            FROM wallet_performance_snapshots
        """
        params: list[Any] = []
        if wallet:
            query += " WHERE wallet = ?"
            params.append(wallet.lower())
        query += " ORDER BY recorded_at DESC, id DESC LIMIT ?"
        params.append(max(int(limit), 0))
        with closing_connection(self.traders_db_path) as conn:
            rows = conn.execute(query, params).fetchall()
        return [
            {
                "wallet": row["wallet"],
                "score": float(row["score"]),
                "confidence": float(row["confidence"]),
                "trend": row["trend"],
                "status": row["status"],
                "metrics": json.loads(row["metrics_json"]),
                "components": json.loads(row["components_json"]),
                "recorded_at": row["recorded_at"],
            }
            for row in rows
        ]

    def load_actions(self, *, action: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        query = """
            SELECT wallet, action, previous_status, new_status, score, reason, recorded_at
            FROM wallet_performance_actions
        """
        params: list[Any] = []
        if action:
            query += " WHERE action = ?"
            params.append(action)
        query += " ORDER BY recorded_at DESC, id DESC LIMIT ?"
        params.append(max(int(limit), 0))
        with closing_connection(self.traders_db_path) as conn:
            rows = conn.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    def latest_scores_by_wallet(self) -> dict[str, WalletPerformanceScore]:
        scores: dict[str, WalletPerformanceScore] = {}
        snapshots = self.load_snapshots(limit=500)
        for row in snapshots:
            wallet = str(row["wallet"])
            if wallet in scores:
                continue
            scores[wallet] = WalletPerformanceScore(
                wallet=wallet,
                score=float(row["score"]),
                confidence=float(row["confidence"]),
                trend=str(row["trend"]),
                status=str(row["status"]),
                metrics=row.get("metrics") or {},
                components=row.get("components") or {},
            )
        return scores

    def performance_boost(self, wallet: str) -> float:
        """Return 0-1 boost factor for discovery/scoring integration."""
        snapshot = self.latest_scores_by_wallet().get(wallet.lower())
        if snapshot is None:
            return 0.0
        if snapshot.status == "promoted":
            return 0.25
        if snapshot.status == "active":
            return 0.15
        if snapshot.status == "probation":
            return 0.0
        return -0.15
