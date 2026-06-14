from __future__ import annotations

from pathlib import Path
from typing import Any

from src.analysis.trader_signal_engine import DEFAULT_TRADER_SIGNAL_DB, init_trader_signal_db
from src.analysis.trader_signal_gates import DEFAULT_MINIMUM_ACCURACY, DEFAULT_MINIMUM_VALIDATIONS
from src.analysis.trader_signal_paper_bridge import init_trader_signal_paper_bridge_db
from src.analysis.trader_signal_validation import init_trader_signal_validation_db
from src.sqlite_utils import closing_connection

MIN_VALIDATIONS = DEFAULT_MINIMUM_VALIDATIONS
MIN_ACCURACY = DEFAULT_MINIMUM_ACCURACY

DASHBOARD_VIEWS: tuple[str, ...] = (
    "v_signal_family_performance",
    "v_gate_status_summary",
    "v_gate_confidence",
    "v_trader_signal_kpis",
    "v_dashboard_overview",
    "v_recommendation_pipeline",
    "v_paper_intent_pipeline",
    "v_trader_leaderboard",
    "v_latest_recommendations",
    "v_latest_paper_intents",
    "v_validation_trend",
    "v_signal_performance_trend",
    "v_recommendation_trend",
    "v_intent_trend",
    "v_database_health",
)

_VIEW_SQL: dict[str, str] = {
    "v_signal_family_performance": f"""
        CREATE VIEW v_signal_family_performance AS
        WITH signal_types(signal_type) AS (
            VALUES
                ('early_entry'),
                ('conviction'),
                ('exit'),
                ('consensus'),
                ('contrarian')
        ),
        family_validations AS (
            SELECT
                signal_type,
                COUNT(*) AS validation_count,
                ROUND(AVG(correct), 6) AS accuracy,
                ROUND(AVG(roi_proxy), 6) AS avg_roi_proxy,
                ROUND(
                    AVG(
                        CASE
                            WHEN resolved_at IS NOT NULL
                                AND resolved_at != ''
                                AND datetime(replace(resolved_at, 'Z', '')) >= datetime('now', '-7 days')
                            THEN correct
                        END
                    ),
                    6
                ) AS rolling_7_day_accuracy,
                ROUND(
                    AVG(
                        CASE
                            WHEN resolved_at IS NOT NULL
                                AND resolved_at != ''
                                AND datetime(replace(resolved_at, 'Z', '')) >= datetime('now', '-30 days')
                            THEN correct
                        END
                    ),
                    6
                ) AS rolling_30_day_accuracy
            FROM trader_signal_validation
            GROUP BY signal_type
        )
        SELECT
            st.signal_type,
            COALESCE(fv.validation_count, 0) AS validation_count,
            fv.accuracy,
            fv.avg_roi_proxy,
            fv.rolling_7_day_accuracy,
            fv.rolling_30_day_accuracy,
            ROUND(
                COALESCE(fv.accuracy, 0.0)
                * MIN(1.0, COALESCE(fv.validation_count, 0) * 1.0 / {MIN_VALIDATIONS})
                * 100.0,
                4
            ) AS confidence_score,
            CASE
                WHEN COALESCE(fv.validation_count, 0) < {MIN_VALIDATIONS} THEN 'unproven'
                WHEN fv.accuracy IS NULL OR fv.accuracy < {MIN_ACCURACY} THEN 'weak'
                ELSE 'proven'
            END AS gate_status,
            CASE
                WHEN COALESCE(fv.validation_count, 0) < {MIN_VALIDATIONS} THEN 0
                WHEN fv.accuracy IS NULL OR fv.accuracy < {MIN_ACCURACY} THEN 0
                ELSE 1
            END AS gate_passes
        FROM signal_types st
        LEFT JOIN family_validations fv ON st.signal_type = fv.signal_type
    """,
    "v_gate_status_summary": """
        CREATE VIEW v_gate_status_summary AS
        SELECT
            COALESCE(SUM(CASE WHEN gate_status = 'proven' THEN 1 ELSE 0 END), 0) AS proven_count,
            COALESCE(SUM(CASE WHEN gate_status = 'unproven' THEN 1 ELSE 0 END), 0) AS unproven_count,
            COALESCE(SUM(CASE WHEN gate_status = 'weak' THEN 1 ELSE 0 END), 0) AS weak_count,
            COALESCE(SUM(CASE WHEN gate_status IN ('unproven', 'weak') THEN 1 ELSE 0 END), 0) AS blocked_count
        FROM v_signal_family_performance
    """,
    "v_gate_confidence": """
        CREATE VIEW v_gate_confidence AS
        SELECT
            signal_type,
            validation_count,
            accuracy,
            confidence_score,
            gate_status,
            gate_passes,
            rolling_7_day_accuracy,
            rolling_30_day_accuracy,
            RANK() OVER (
                ORDER BY confidence_score DESC, validation_count DESC, signal_type ASC
            ) AS confidence_rank
        FROM v_signal_family_performance
        ORDER BY confidence_rank ASC, signal_type ASC
    """,
    "v_dashboard_overview": f"""
        CREATE VIEW v_dashboard_overview AS
        SELECT
            COALESCE((SELECT COUNT(*) FROM trader_signals), 0) AS total_signals,
            COALESCE(
                (SELECT ROUND(AVG(correct), 6) FROM trader_signal_validation),
                0.0
            ) AS validation_accuracy,
            COALESCE(
                (
                    SELECT COUNT(*)
                    FROM v_signal_family_performance
                    WHERE gate_status = 'proven'
                ),
                0
            ) AS proven_families,
            COALESCE(
                (
                    SELECT COUNT(*)
                    FROM trader_signal_recommendations
                    WHERE recommendation_type = 'blocked'
                ),
                0
            ) AS blocked_recommendations,
            COALESCE(
                (
                    SELECT COUNT(*)
                    FROM trader_signal_paper_intents
                    WHERE status = 'simulated'
                ),
                0
            ) AS simulated_paper_intents,
            COALESCE((SELECT COUNT(*) FROM trader_signal_validation), 0) AS validated_signals,
            COALESCE((SELECT COUNT(*) FROM trader_signal_recommendations), 0) AS total_recommendations,
            COALESCE(
                (
                    SELECT ROUND(SUM(notional_usd), 4)
                    FROM trader_signal_paper_intents
                    WHERE status = 'simulated'
                ),
                0.0
            ) AS simulated_notional_usd,
            {MIN_VALIDATIONS} AS minimum_validations,
            {MIN_ACCURACY} AS minimum_accuracy,
            1 AS read_only,
            1 AS paper_only
    """,
    "v_trader_signal_kpis": f"""
        CREATE VIEW v_trader_signal_kpis AS
        SELECT
            COALESCE((SELECT COUNT(*) FROM trader_signals), 0) AS total_signals,
            COALESCE((SELECT COUNT(*) FROM trader_signal_validation), 0) AS validated_signals,
            COALESCE(
                (
                    SELECT COUNT(*)
                    FROM v_signal_family_performance
                    WHERE gate_status = 'proven'
                ),
                0
            ) AS proven_families,
            COALESCE(
                (
                    SELECT COUNT(*)
                    FROM trader_signal_recommendations
                    WHERE recommendation_type = 'blocked'
                ),
                0
            ) AS blocked_recommendations,
            COALESCE((SELECT COUNT(*) FROM trader_signal_paper_intents), 0) AS paper_intents,
            {MIN_VALIDATIONS} AS minimum_validations,
            {MIN_ACCURACY} AS minimum_accuracy,
            1 AS read_only,
            1 AS paper_only
    """,
    "v_recommendation_pipeline": """
        CREATE VIEW v_recommendation_pipeline AS
        SELECT
            COALESCE((SELECT COUNT(*) FROM trader_signal_recommendations), 0) AS total_recommendations,
            COALESCE(
                (
                    SELECT COUNT(*)
                    FROM trader_signal_recommendations
                    WHERE recommendation_type != 'blocked'
                ),
                0
            ) AS promoted_count,
            COALESCE(
                (
                    SELECT COUNT(*)
                    FROM trader_signal_recommendations
                    WHERE recommendation_type = 'blocked'
                ),
                0
            ) AS blocked_count,
            COALESCE(
                (
                    SELECT COUNT(*)
                    FROM trader_signal_recommendations
                    WHERE recommendation_type = 'watch'
                ),
                0
            ) AS watch_count,
            COALESCE(
                (
                    SELECT COUNT(*)
                    FROM trader_signal_recommendations
                    WHERE recommendation_type = 'paper_entry'
                ),
                0
            ) AS paper_entry_count,
            COALESCE(
                (
                    SELECT COUNT(*)
                    FROM trader_signal_recommendations
                    WHERE recommendation_type = 'paper_exit'
                ),
                0
            ) AS paper_exit_count,
            COALESCE(
                (
                    SELECT COUNT(*)
                    FROM trader_signal_recommendations
                    WHERE recommendation_type = 'avoid'
                ),
                0
            ) AS avoid_count
    """,
    "v_paper_intent_pipeline": """
        CREATE VIEW v_paper_intent_pipeline AS
        SELECT
            COALESCE((SELECT COUNT(*) FROM trader_signal_paper_intents), 0) AS total_intents,
            COALESCE(
                (
                    SELECT COUNT(*)
                    FROM trader_signal_paper_intents
                    WHERE status = 'candidate'
                ),
                0
            ) AS candidate_count,
            COALESCE(
                (
                    SELECT COUNT(*)
                    FROM trader_signal_paper_intents
                    WHERE status = 'simulated'
                ),
                0
            ) AS simulated_count,
            COALESCE(
                (
                    SELECT COUNT(*)
                    FROM trader_signal_paper_intents
                    WHERE status = 'blocked'
                ),
                0
            ) AS blocked_count
    """,
    "v_trader_leaderboard": """
        CREATE VIEW v_trader_leaderboard AS
        SELECT
            trader_address,
            COUNT(*) AS validation_count,
            SUM(correct) AS correct_count,
            COUNT(*) - SUM(correct) AS incorrect_count,
            ROUND(AVG(correct), 6) AS accuracy,
            ROUND(AVG(roi_proxy), 6) AS avg_roi_proxy,
            CASE
                WHEN COUNT(*) >= 5 AND AVG(correct) >= 0.55 THEN 'top'
                WHEN COUNT(*) >= 5 AND AVG(correct) < 0.45 THEN 'weak'
                ELSE 'neutral'
            END AS trader_tier
        FROM trader_signal_validation
        GROUP BY trader_address
        ORDER BY accuracy DESC, validation_count DESC, trader_address ASC
    """,
    "v_latest_recommendations": """
        CREATE VIEW v_latest_recommendations AS
        SELECT
            r.recommendation_key,
            r.wallet,
            r.market_id,
            r.recommendation_type,
            r.signal_type,
            r.score,
            r.reason,
            r.conflict,
            r.created_at,
            COALESCE(f.gate_status, 'unproven') AS gate_status,
            f.accuracy AS historical_accuracy,
            COALESCE(f.validation_count, 0) AS validation_count,
            COALESCE(f.confidence_score, 0.0) AS confidence_score,
            CASE WHEN r.recommendation_type = 'blocked' THEN 1 ELSE 0 END AS is_blocked
        FROM trader_signal_recommendations r
        LEFT JOIN v_signal_family_performance f ON r.signal_type = f.signal_type
        ORDER BY r.created_at DESC, r.recommendation_key ASC
        LIMIT 50
    """,
    "v_latest_paper_intents": """
        CREATE VIEW v_latest_paper_intents AS
        SELECT
            intent_id,
            intent_key,
            recommendation_id,
            market_id,
            side,
            recommendation_type,
            signal_type,
            trader_address,
            score,
            validation_count,
            historical_accuracy,
            gate_status,
            gate_reason,
            notional_usd,
            status,
            reason,
            created_at,
            read_only,
            paper_only
        FROM trader_signal_paper_intents
        ORDER BY created_at DESC, intent_id DESC
        LIMIT 50
    """,
    "v_validation_trend": """
        CREATE VIEW v_validation_trend AS
        SELECT
            COALESCE(
                date(replace(replace(resolved_at, 'T', ' '), 'Z', '')),
                date(replace(replace(validation_timestamp, 'T', ' '), 'Z', ''))
            ) AS validation_date,
            COUNT(*) AS validation_count,
            SUM(correct) AS correct_count,
            ROUND(AVG(correct), 6) AS accuracy,
            ROUND(AVG(roi_proxy), 6) AS avg_roi_proxy
        FROM trader_signal_validation
        GROUP BY validation_date
        ORDER BY validation_date ASC
    """,
    "v_signal_performance_trend": """
        CREATE VIEW v_signal_performance_trend AS
        SELECT
            COALESCE(
                date(replace(replace(created_at, 'T', ' '), 'Z', '')),
                date('1970-01-01')
            ) AS trend_date,
            signal_type,
            COUNT(*) AS signal_count,
            ROUND(AVG(score), 4) AS avg_score
        FROM trader_signals
        GROUP BY trend_date, signal_type
        ORDER BY trend_date ASC, signal_type ASC
    """,
    "v_recommendation_trend": """
        CREATE VIEW v_recommendation_trend AS
        SELECT
            COALESCE(
                date(replace(replace(created_at, 'T', ' '), 'Z', '')),
                date('1970-01-01')
            ) AS trend_date,
            COUNT(*) AS recommendation_count,
            SUM(CASE WHEN recommendation_type = 'blocked' THEN 1 ELSE 0 END) AS blocked_count,
            SUM(CASE WHEN recommendation_type != 'blocked' THEN 1 ELSE 0 END) AS promoted_count
        FROM trader_signal_recommendations
        GROUP BY trend_date
        ORDER BY trend_date ASC
    """,
    "v_intent_trend": """
        CREATE VIEW v_intent_trend AS
        SELECT
            COALESCE(
                date(replace(replace(created_at, 'T', ' '), 'Z', '')),
                date('1970-01-01')
            ) AS trend_date,
            COUNT(*) AS intent_count,
            SUM(CASE WHEN status = 'candidate' THEN 1 ELSE 0 END) AS candidate_count,
            SUM(CASE WHEN status = 'simulated' THEN 1 ELSE 0 END) AS simulated_count,
            SUM(CASE WHEN status = 'blocked' THEN 1 ELSE 0 END) AS blocked_count,
            ROUND(COALESCE(SUM(notional_usd), 0.0), 4) AS total_notional_usd,
            ROUND(
                COALESCE(SUM(CASE WHEN status = 'simulated' THEN notional_usd ELSE 0 END), 0.0),
                4
            ) AS simulated_notional_usd
        FROM trader_signal_paper_intents
        GROUP BY trend_date
        ORDER BY trend_date ASC
    """,
    "v_database_health": """
        CREATE VIEW v_database_health AS
        SELECT
            COALESCE((SELECT COUNT(*) FROM trader_signals), 0) AS signal_rows,
            COALESCE((SELECT COUNT(*) FROM trader_signal_validation), 0) AS validation_rows,
            COALESCE((SELECT COUNT(*) FROM trader_signal_recommendations), 0) AS recommendation_rows,
            COALESCE((SELECT COUNT(*) FROM trader_signal_paper_intents), 0) AS intent_rows,
            (SELECT MAX(created_at) FROM trader_signals) AS latest_signal_at,
            (SELECT MAX(validation_timestamp) FROM trader_signal_validation) AS latest_validation_at,
            (SELECT MAX(created_at) FROM trader_signal_recommendations) AS latest_recommendation_at,
            (SELECT MAX(created_at) FROM trader_signal_paper_intents) AS latest_intent_at,
            1 AS read_only,
            1 AS paper_only
    """,
}


def _with_flags(payload: dict[str, Any]) -> dict[str, Any]:
    return {"read_only": True, "paper_only": True, **payload}


def init_trader_signal_dashboard_views(db_path: str | Path = DEFAULT_TRADER_SIGNAL_DB) -> list[str]:
    init_trader_signal_db(db_path)
    init_trader_signal_validation_db(db_path)
    init_trader_signal_paper_bridge_db(db_path)
    with closing_connection(db_path) as conn:
        for view_name in DASHBOARD_VIEWS:
            conn.execute(f"DROP VIEW IF EXISTS {view_name}")
        for view_name in DASHBOARD_VIEWS:
            conn.execute(_VIEW_SQL[view_name])
    return list(DASHBOARD_VIEWS)


def trader_signal_dashboard_views_report(
    *,
    db_path: str | Path = DEFAULT_TRADER_SIGNAL_DB,
) -> dict[str, Any]:
    views_created = init_trader_signal_dashboard_views(db_path)
    return _with_flags(
        {
            "db_path": str(db_path),
            "views_created": views_created,
        }
    )
