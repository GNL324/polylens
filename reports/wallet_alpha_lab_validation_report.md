# Wallet Alpha Lab — Validation Report

Branch: `feature/wallet-alpha-lab`  
Date: 2026-06-15

## Architecture Summary

The Wallet Alpha Lab is a research and validation framework built on existing paper copy, signal validation, and performance data. It measures predictive edge, compares baselines, analyzes signal decay, validates promotions, and ranks wallets by alpha grade.

```
Paper Copy + Signal Validation + Performance Snapshots
        │
        ▼
WalletAlphaLab → WalletAlphaReport, AlphaScore
        │
        ├── wallet_baseline_analysis.py (cohort comparisons)
        ├── wallet_signal_decay.py (half-life, persistence)
        └── promotion_validation + archetype_analysis
        │
        ▼
WalletAutonomyService alpha cycle (24h) + Dashboard + CLI
```

## Methodology

### Alpha Calculation

`WalletAlphaReport.alpha_score` combines:
- Performance engine score (35%)
- Win rate from paper copy (20%)
- Signal validation accuracy (20%)
- Excess return vs zero baseline (15%)
- Risk-adjusted Sharpe-like metric (10%)

`AlphaScore` adds factors: performance, persistence, consistency, signal quality, risk-adjusted return, decay resistance — graded A through F.

### Baseline Methodology

Cohorts compared: random selection, average discovered, average promoted, average active, paper trading aggregate. Metrics: ROI, expectancy, win rate, drawdown, Sharpe-like proxy.

### Decay Methodology

Derived from `wallet_performance_trends` score deltas and snapshot history. Computes signal half-life, decay rate, performance persistence, time-to-degradation.

## Reused Components

- `WalletPerformanceEngine`, `WalletDiscoveryEngine`
- `wallet_signal_analytics` performance rows and validation stats
- `paper_copy_trader` realized outcomes
- `trader_registry`, strategy profiles
- `WalletAutonomyService` cycle infrastructure
- Trader dashboard

## Test Results

Full Polylens suite run on completion. Tests cover alpha scoring, baselines, decay, promotion validation, dashboard, CLI, and autonomy integration.

## Critical Research Answers

### 1. Are promoted wallets outperforming average wallets?

Measured via `promotion_validation.promotion_justified` — compares promoted cohort average alpha against average discovered wallet alpha. Reported in `research_answers.promoted_outperform_average`.

### 2. Are discovered wallets generating useful signals?

Measured via `wallet_baseline_analysis_report.discovered_signal_useful` — true when average signal validation accuracy exceeds 50%.

### 3. Which archetypes produce the most alpha?

`archetype_analysis()` ranks archetypes by `avg_alpha_score` from paper outcomes and performance data. Top archetype reported in `research_answers.top_alpha_archetype`.

### 4. How quickly do wallet signals decay?

`wallet_decay_report` computes `avg_half_life_days` and per-wallet `decay_rate` from performance trend history.

### 5. Is the wallet intelligence system improving over time?

`wallet_baseline_analysis_report.system_improving` compares promoted cohort alpha across consecutive alpha lab runs.

## Limitations

- Alpha scores depend on paper copy sample size; wallets with fewer than 3 samples marked `insufficient_data`
- Decay analysis requires performance trend history; new deployments have limited decay data
- Baseline comparisons use available paper copy cohorts; sparse data reduces statistical confidence

## Future Work

- Walk-forward backtesting on historical signal validation
- Confidence intervals on promotion validation
- Alpha lab run comparison charts in dashboard

## Success Criteria

Paper-only research framework with quantifiable alpha, baselines, decay, promotion validation, dashboard visibility, autonomy integration, full tests, and documentation.
