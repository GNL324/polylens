"""Wallet Intelligence Layer — thin orchestration over existing Polylens analytics."""

from src.intelligence.signal_engine import SignalEngine, run_wallet_intelligence_cycle
from src.intelligence.strategy_classifier import StrategyClassifier, StrategyProfile
from src.intelligence.wallet_alpha_lab import WalletAlphaLab, init_wallet_alpha_lab_db, run_wallet_alpha_lab_cycle
from src.intelligence.wallet_acquisition_analytics import (
    wallet_acquisition_analytics_report,
    wallet_acquisition_health_summary,
    wallet_registry_growth_report,
    wallet_source_stats_report,
)
from src.intelligence.wallet_autonomy_service import (
    WalletAutonomyService,
    init_wallet_service_state_db,
    load_wallet_autonomy_reports,
    run_wallet_autonomy_service,
    wallet_autonomy_report,
)
from src.intelligence.wallet_baseline_analysis import wallet_baseline_analysis_report
from src.intelligence.wallet_data_acquisition import (
    WalletDataAcquisitionEngine,
    init_wallet_acquisition_db,
    run_wallet_acquisition,
)
from src.intelligence.wallet_discovery import WalletDiscoveryConfig, WalletDiscoveryEngine
from src.intelligence.wallet_discovery_analytics import wallet_discovery_analytics_report
from src.intelligence.wallet_feedback_engine import FeedbackConfig, WalletFeedbackEngine, run_wallet_feedback_cycle
from src.intelligence.wallet_performance import WalletPerformanceEngine, WalletPerformanceScore, init_wallet_performance_db
from src.intelligence.wallet_performance_analytics import wallet_performance_analytics_report
from src.intelligence.wallet_quality_filter import WalletQualityFilter, WalletQualityScore
from src.intelligence.wallet_scoring import WalletScore, WalletScorer
from src.intelligence.wallet_service_health import wallet_service_health_summary
from src.intelligence.wallet_signal_analytics import wallet_signal_analytics_report
from src.intelligence.wallet_signal_decay import wallet_decay_report
from src.intelligence.wallet_signal_integration import run_wallet_signal_integration_cycle
from src.intelligence.wallet_tracker import WalletTracker, WalletWatchlistEntry

__all__ = [
    "FeedbackConfig",
    "SignalEngine",
    "StrategyClassifier",
    "StrategyProfile",
    "WalletAutonomyService",
    "WalletAlphaLab",
    "WalletDataAcquisitionEngine",
    "WalletDiscoveryConfig",
    "WalletDiscoveryEngine",
    "WalletFeedbackEngine",
    "WalletPerformanceEngine",
    "WalletPerformanceScore",
    "WalletQualityFilter",
    "WalletQualityScore",
    "WalletScore",
    "WalletScorer",
    "WalletTracker",
    "WalletWatchlistEntry",
    "init_wallet_alpha_lab_db",
    "init_wallet_acquisition_db",
    "init_wallet_performance_db",
    "init_wallet_service_state_db",
    "load_wallet_autonomy_reports",
    "run_wallet_alpha_lab_cycle",
    "run_wallet_acquisition",
    "run_wallet_autonomy_service",
    "run_wallet_feedback_cycle",
    "run_wallet_intelligence_cycle",
    "run_wallet_signal_integration_cycle",
    "wallet_acquisition_analytics_report",
    "wallet_acquisition_health_summary",
    "wallet_autonomy_report",
    "wallet_baseline_analysis_report",
    "wallet_decay_report",
    "wallet_discovery_analytics_report",
    "wallet_performance_analytics_report",
    "wallet_registry_growth_report",
    "wallet_service_health_summary",
    "wallet_signal_analytics_report",
    "wallet_source_stats_report",
]
