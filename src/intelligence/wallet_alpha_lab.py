from __future__ import annotations

import json
import math
import statistics
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.analysis.paper_copy_trader import DEFAULT_PAPER_COPY_DB, paper_copy_report
from src.analysis.trader_discovery import DEFAULT_TRADER_DISCOVERY_DB
from src.analysis.trader_registry import DEFAULT_TRADERS_DB, list_traders
from src.analysis.trader_signal_engine import DEFAULT_TRADER_SIGNAL_DB
from src.intelligence.wallet_discovery import WalletDiscoveryEngine, init_wallet_discovery_db
from src.intelligence.wallet_performance import WalletPerformanceEngine, init_wallet_performance_db
from src.intelligence.wallet_signal_analytics import _load_strategy_profiles, _wallet_validation_stats, wallet_performance_rows
from src.sqlite_utils import closing_connection

ALPHA_GRADES = ("A", "B", "C", "D", "F")
ALPHA_STATUSES = ("strong", "developing", "weak", "insufficient_data")


@dataclass
class WalletAlphaReport:
    wallet: str
    alpha_score: float
    confidence: float
    sample_size: int
    status: str
    metrics: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class AlphaScore:
    wallet: str
    alpha_rank: int
    alpha_score: float
    alpha_confidence: float
    alpha_grade: str
    factors: dict[str, float] = field(default_factory=dict)

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


def _grade_from_score(score: float) -> str:
    if score >= 80:
        return "A"
    if score >= 65:
        return "B"
    if score >= 50:
        return "C"
    if score >= 35:
        return "D"
    return "F"


def _with_flags(payload: dict[str, Any]) -> dict[str, Any]:
    return {"read_only": True, "paper_only": True, **payload}


def init_wallet_alpha_lab_db(
    traders_db_path: str | Path = DEFAULT_TRADERS_DB,
    discovery_db_path: str | Path = DEFAULT_TRADER_DISCOVERY_DB,
) -> None:
    init_wallet_discovery_db(traders_db_path, discovery_db_path)
    init_wallet_performance_db(traders_db_path)
    with closing_connection(traders_db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS wallet_alpha_reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                wallet TEXT NOT NULL,
                alpha_score REAL NOT NULL,
                confidence REAL NOT NULL,
                sample_size INTEGER NOT NULL,
                status TEXT NOT NULL,
                metrics_json TEXT NOT NULL,
                recorded_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS wallet_alpha_rankings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                wallet TEXT NOT NULL,
                alpha_rank INTEGER NOT NULL,
                alpha_score REAL NOT NULL,
                alpha_confidence REAL NOT NULL,
                alpha_grade TEXT NOT NULL,
                factors_json TEXT NOT NULL,
                recorded_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS wallet_alpha_lab_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_type TEXT NOT NULL,
                summary_json TEXT NOT NULL,
                recorded_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_wallet_alpha_reports_wallet ON wallet_alpha_reports(wallet, recorded_at DESC);
            CREATE INDEX IF NOT EXISTS idx_wallet_alpha_rankings_time ON wallet_alpha_rankings(recorded_at DESC, alpha_rank ASC);
            """
        )


class WalletAlphaLab:
    """Research framework measuring wallet predictive edge from paper and signal outcomes."""

    def __init__(
        self,
        *,
        traders_db_path: str | Path = DEFAULT_TRADERS_DB,
        discovery_db_path: str | Path = DEFAULT_TRADER_DISCOVERY_DB,
        signal_db_path: str | Path = DEFAULT_TRADER_SIGNAL_DB,
        paper_copy_db_path: str | Path = DEFAULT_PAPER_COPY_DB,
    ) -> None:
        self.traders_db_path = Path(traders_db_path)
        self.discovery_db_path = Path(discovery_db_path)
        self.signal_db_path = Path(signal_db_path)
        self.paper_copy_db_path = Path(paper_copy_db_path)
        self._discovery = WalletDiscoveryEngine(traders_db_path=traders_db_path, discovery_db_path=discovery_db_path)
        self._performance = WalletPerformanceEngine(
            traders_db_path=traders_db_path,
            discovery_db_path=discovery_db_path,
            paper_copy_db_path=paper_copy_db_path,
            signal_db_path=signal_db_path,
        )
        init_wallet_alpha_lab_db(traders_db_path, discovery_db_path)

    def _wallet_rows(self, limit: int = 50) -> list[dict[str, Any]]:
        return wallet_performance_rows(
            paper_copy_db_path=self.paper_copy_db_path,
            traders_db_path=self.traders_db_path,
            signal_db_path=self.signal_db_path,
            limit=limit,
        )

    def analyze_wallet(self, wallet: str) -> WalletAlphaReport:
        wallet = str(wallet or "").strip().lower()
        perf = self._performance.score_wallet(wallet)
        validations = _wallet_validation_stats(self.signal_db_path).get(wallet, {})
        copy = paper_copy_report(db_path=self.paper_copy_db_path).get("by_wallet", {}).get(wallet, {})
        closed = int(copy.get("closed_positions") or 0)
        roi = _safe_float(copy.get("roi"))
        win_rate = _safe_float(copy.get("win_rate") if copy.get("win_rate") is not None else perf.metrics.get("win_rate"))
        accuracy = _safe_float(validations.get("accuracy"))
        sample_size = closed + int(validations.get("validation_count") or 0)

        excess_return = roi - 0.0
        risk_adjusted = _safe_float(perf.metrics.get("sharpe_like"))
        prediction_quality = accuracy
        realized_performance = roi

        alpha_score = _clamp(
            perf.score * 0.35
            + _clamp(win_rate * 100) * 0.20
            + _clamp(accuracy * 100) * 0.20
            + _clamp(excess_return * 100 + 50) * 0.15
            + _clamp(risk_adjusted * 25 + 50) * 0.10
        )
        confidence = _clamp(math.log1p(sample_size) / math.log1p(50) * perf.confidence, 0, 1)
        if sample_size < 3:
            status = "insufficient_data"
        elif alpha_score >= 65:
            status = "strong"
        elif alpha_score >= 45:
            status = "developing"
        else:
            status = "weak"

        return WalletAlphaReport(
            wallet=wallet,
            alpha_score=round(alpha_score, 4),
            confidence=round(confidence, 4),
            sample_size=sample_size,
            status=status,
            metrics={
                "excess_return": round(excess_return, 4),
                "risk_adjusted_return": round(risk_adjusted, 4),
                "signal_accuracy": round(accuracy, 4) if accuracy else None,
                "prediction_quality": round(prediction_quality, 4) if prediction_quality else None,
                "realized_performance": round(realized_performance, 4) if closed else None,
                "win_rate": round(win_rate, 4) if win_rate else None,
                "performance_score": perf.score,
                "trend": perf.trend,
            },
        )

    def compute_alpha_score(self, report: WalletAlphaReport, *, decay_resistance: float = 50.0) -> AlphaScore:
        metrics = report.metrics
        factors = {
            "performance": _clamp(_safe_float(metrics.get("realized_performance")) * 100 + 50) if metrics.get("realized_performance") else 40.0,
            "persistence": 70.0 if metrics.get("trend") == "improving" else 50.0 if metrics.get("trend") == "stable" else 30.0,
            "consistency": _clamp(_safe_float(metrics.get("win_rate")) * 100) if metrics.get("win_rate") else 40.0,
            "signal_quality": _clamp(_safe_float(metrics.get("signal_accuracy")) * 100) if metrics.get("signal_accuracy") else 40.0,
            "risk_adjusted_return": _clamp(_safe_float(metrics.get("risk_adjusted_return")) * 25 + 50),
            "decay_resistance": _clamp(decay_resistance),
        }
        composite = round(
            _clamp(
                factors["performance"] * 0.22
                + factors["persistence"] * 0.15
                + factors["consistency"] * 0.18
                + factors["signal_quality"] * 0.18
                + factors["risk_adjusted_return"] * 0.17
                + factors["decay_resistance"] * 0.10
            ),
            4,
        )
        return AlphaScore(
            wallet=report.wallet,
            alpha_rank=0,
            alpha_score=composite,
            alpha_confidence=report.confidence,
            alpha_grade=_grade_from_score(composite),
            factors={k: round(v, 4) for k, v in factors.items()},
        )

    def rank_wallets(self, wallets: list[str] | None = None, *, limit: int = 25) -> list[AlphaScore]:
        if wallets is None:
            lifecycle = self._discovery.load_lifecycle(limit=limit * 2)
            wallets = [str(row["wallet"]) for row in lifecycle]
            if not wallets:
                wallets = [row.wallet for row in list_traders(limit=limit, db_path=str(self.traders_db_path))]
        from src.intelligence.wallet_signal_decay import wallet_decay_report

        decay_data = {row["wallet"]: row for row in wallet_decay_report(traders_db_path=self.traders_db_path).get("wallets", [])}
        ranked: list[AlphaScore] = []
        for wallet in wallets[: limit * 2]:
            report = self.analyze_wallet(wallet)
            decay = decay_data.get(wallet, {})
            resistance = 100.0 - _clamp(_safe_float(decay.get("decay_rate")) * 100)
            score = self.compute_alpha_score(report, decay_resistance=resistance)
            ranked.append(score)
        ranked.sort(key=lambda row: (-row.alpha_score, -row.alpha_confidence, row.wallet))
        result: list[AlphaScore] = []
        for index, row in enumerate(ranked[:limit], start=1):
            result.append(
                AlphaScore(
                    wallet=row.wallet,
                    alpha_rank=index,
                    alpha_score=row.alpha_score,
                    alpha_confidence=row.alpha_confidence,
                    alpha_grade=row.alpha_grade,
                    factors=row.factors,
                )
            )
        return result

    def archetype_analysis(self, *, limit: int = 25) -> list[dict[str, Any]]:
        profiles = _load_strategy_profiles(self.traders_db_path)
        rows = self._wallet_rows(limit=limit * 3)
        buckets: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            archetype = profiles.get(str(row["wallet"]), {}).get("archetype", "unknown")
            buckets.setdefault(archetype, []).append(row)
        analysis: list[dict[str, Any]] = []
        for archetype, group in sorted(buckets.items()):
            rois = [_safe_float(item.get("avg_roi")) for item in group if item.get("avg_roi") is not None]
            win_rates = [_safe_float(item.get("win_rate")) for item in group if item.get("win_rate") is not None]
            avg_alpha = statistics.mean([self.analyze_wallet(str(item["wallet"])).alpha_score for item in group]) if group else 0.0
            from src.intelligence.wallet_signal_decay import wallet_decay_report

            decay_rows = wallet_decay_report(traders_db_path=self.traders_db_path).get("by_archetype", {})
            decay_rate = _safe_float((decay_rows.get(archetype) or {}).get("avg_decay_rate"))
            analysis.append(
                {
                    "archetype": archetype,
                    "wallet_count": len(group),
                    "avg_roi": round(statistics.mean(rois), 4) if rois else None,
                    "avg_win_rate": round(statistics.mean(win_rates), 4) if win_rates else None,
                    "avg_alpha_score": round(avg_alpha, 4),
                    "avg_decay_rate": round(decay_rate, 4) if decay_rate else None,
                    "deserves_promotion": avg_alpha >= 55 and (statistics.mean(win_rates) if win_rates else 0) >= 0.5,
                }
            )
        analysis.sort(key=lambda row: -float(row.get("avg_alpha_score") or 0))
        return analysis[:limit]

    def promotion_validation(self) -> dict[str, Any]:
        with closing_connection(self.traders_db_path) as conn:
            promoted = conn.execute(
                """
                SELECT wallet, score FROM wallet_performance_actions
                WHERE action = 'promoted'
                ORDER BY recorded_at DESC LIMIT 100
                """
            ).fetchall()
            demoted = conn.execute(
                """
                SELECT wallet, score FROM wallet_performance_actions
                WHERE action = 'demoted'
                ORDER BY recorded_at DESC LIMIT 100
                """
            ).fetchall()
            retired = conn.execute(
                """
                SELECT wallet, score FROM wallet_performance_actions
                WHERE action = 'retired'
                ORDER BY recorded_at DESC LIMIT 100
                """
            ).fetchall()

        def _cohort_stats(wallets: list[str]) -> dict[str, Any]:
            if not wallets:
                return {"count": 0, "avg_alpha": 0.0, "avg_roi": 0.0}
            alphas = [self.analyze_wallet(w).alpha_score for w in wallets[:30]]
            copy = paper_copy_report(db_path=self.paper_copy_db_path).get("by_wallet", {})
            rois = [_safe_float(copy.get(w, {}).get("roi")) for w in wallets[:30] if copy.get(w)]
            return {
                "count": len(wallets),
                "avg_alpha": round(statistics.mean(alphas), 4) if alphas else 0.0,
                "avg_roi": round(statistics.mean(rois), 4) if rois else 0.0,
            }

        promoted_wallets = list({str(row["wallet"]) for row in promoted})
        demoted_wallets = list({str(row["wallet"]) for row in demoted})
        retired_wallets = list({str(row["wallet"]) for row in retired})
        all_rows = self._wallet_rows(limit=100)
        avg_discovered_alpha = statistics.mean([self.analyze_wallet(str(r["wallet"])).alpha_score for r in all_rows]) if all_rows else 0.0

        promoted_stats = _cohort_stats(promoted_wallets)
        demoted_stats = _cohort_stats(demoted_wallets)
        retired_stats = _cohort_stats(retired_wallets)

        return {
            "promoted": promoted_stats,
            "demoted": demoted_stats,
            "retired": retired_stats,
            "average_discovered_alpha": round(avg_discovered_alpha, 4),
            "promotion_justified": promoted_stats["avg_alpha"] > avg_discovered_alpha,
            "demotion_justified": demoted_stats["avg_alpha"] < avg_discovered_alpha,
            "retirement_justified": retired_stats["avg_alpha"] < avg_discovered_alpha,
        }

    def persist_report(self, report: WalletAlphaReport) -> None:
        with closing_connection(self.traders_db_path) as conn:
            conn.execute(
                """
                INSERT INTO wallet_alpha_reports (
                    wallet, alpha_score, confidence, sample_size, status, metrics_json, recorded_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    report.wallet,
                    report.alpha_score,
                    report.confidence,
                    report.sample_size,
                    report.status,
                    json.dumps(report.metrics, sort_keys=True),
                    _utc_now(),
                ),
            )

    def persist_rankings(self, rankings: list[AlphaScore]) -> None:
        now = _utc_now()
        with closing_connection(self.traders_db_path) as conn:
            for row in rankings:
                conn.execute(
                    """
                    INSERT INTO wallet_alpha_rankings (
                        wallet, alpha_rank, alpha_score, alpha_confidence, alpha_grade, factors_json, recorded_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        row.wallet,
                        row.alpha_rank,
                        row.alpha_score,
                        row.alpha_confidence,
                        row.alpha_grade,
                        json.dumps(row.factors, sort_keys=True),
                        now,
                    ),
                )

    def load_recent_rankings(self, *, limit: int = 25) -> list[dict[str, Any]]:
        with closing_connection(self.traders_db_path) as conn:
            rows = conn.execute(
                """
                SELECT wallet, alpha_rank, alpha_score, alpha_confidence, alpha_grade, factors_json, recorded_at
                FROM wallet_alpha_rankings
                ORDER BY recorded_at DESC, alpha_rank ASC
                LIMIT ?
                """,
                (max(int(limit), 0) * 3,),
            ).fetchall()
        seen: set[str] = set()
        result: list[dict[str, Any]] = []
        for row in rows:
            wallet = str(row["wallet"])
            if wallet in seen:
                continue
            seen.add(wallet)
            result.append(
                {
                    **dict(row),
                    "factors": json.loads(row["factors_json"] or "{}"),
                }
            )
            if len(result) >= limit:
                break
        return result

    def load_alpha_trends(self, *, wallet: str | None = None, limit: int = 25) -> list[dict[str, Any]]:
        query = """
            SELECT wallet, alpha_score, confidence, sample_size, status, recorded_at
            FROM wallet_alpha_reports
        """
        params: list[Any] = []
        if wallet:
            query += " WHERE wallet = ?"
            params.append(wallet.lower())
        query += " ORDER BY recorded_at DESC, id DESC LIMIT ?"
        params.append(max(int(limit), 0))
        with closing_connection(self.traders_db_path) as conn:
            rows = conn.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    def run_alpha_lab_cycle(self, *, limit: int = 25) -> dict[str, Any]:
        from src.intelligence.wallet_baseline_analysis import wallet_baseline_analysis_report
        from src.intelligence.wallet_signal_decay import wallet_decay_report

        rankings = self.rank_wallets(limit=limit)
        for row in rankings:
            self.persist_report(self.analyze_wallet(row.wallet))
        self.persist_rankings(rankings)
        baselines = wallet_baseline_analysis_report(
            traders_db_path=self.traders_db_path,
            discovery_db_path=self.discovery_db_path,
            paper_copy_db_path=self.paper_copy_db_path,
            signal_db_path=self.signal_db_path,
        )
        decay = wallet_decay_report(
            traders_db_path=self.traders_db_path,
            paper_copy_db_path=self.paper_copy_db_path,
            signal_db_path=self.signal_db_path,
        )
        archetypes = self.archetype_analysis(limit=limit)
        promotion = self.promotion_validation()
        summary = _with_flags(
            {
                "generated_at": _utc_now(),
                "rankings_count": len(rankings),
                "top_alpha_wallets": [row.to_dict() for row in rankings[:10]],
                "baselines": baselines,
                "decay_summary": {
                    "avg_half_life_days": decay.get("avg_half_life_days"),
                    "avg_decay_rate": decay.get("avg_decay_rate"),
                },
                "archetype_analysis": archetypes[:10],
                "promotion_validation": promotion,
                "research_answers": _research_answers(rankings, baselines, decay, archetypes, promotion),
            }
        )
        with closing_connection(self.traders_db_path) as conn:
            conn.execute(
                """
                INSERT INTO wallet_alpha_lab_runs (run_type, summary_json, recorded_at)
                VALUES (?, ?, ?)
                """,
                ("full_cycle", json.dumps(summary, sort_keys=True), _utc_now()),
            )
        return summary


def _research_answers(
    rankings: list[AlphaScore],
    baselines: dict[str, Any],
    decay: dict[str, Any],
    archetypes: list[dict[str, Any]],
    promotion: dict[str, Any],
) -> dict[str, Any]:
    top_archetype = archetypes[0]["archetype"] if archetypes else "unknown"
    return {
        "promoted_outperform_average": promotion.get("promotion_justified", False),
        "discovered_signals_useful": baselines.get("discovered_signal_useful", False),
        "top_alpha_archetype": top_archetype,
        "avg_signal_half_life_days": decay.get("avg_half_life_days"),
        "system_improving": baselines.get("system_improving", False),
    }


def run_wallet_alpha_lab_cycle(
    *,
    traders_db_path: str | Path = DEFAULT_TRADERS_DB,
    discovery_db_path: str | Path = DEFAULT_TRADER_DISCOVERY_DB,
    limit: int = 25,
) -> dict[str, Any]:
    return WalletAlphaLab(traders_db_path=traders_db_path, discovery_db_path=discovery_db_path).run_alpha_lab_cycle(limit=limit)
