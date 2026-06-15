# Wallet Alpha Lab — Research Plan

Branch: `feature/wallet-alpha-lab`

## Objective

Build a research and validation framework that measures whether discovered, promoted, and tracked wallets generate predictive edge and outperform baseline strategies — paper-only, analytics-only.

## Research Audit

### Available Datasets

| Dataset | Location | Metrics |
|---|---|---|
| Paper copy positions | `paper_copy_positions` | ROI, PnL, win rate, closed positions |
| Trader signal validation | `trader_signal_validation` | Accuracy, roi_proxy |
| Wallet performance snapshots | `wallet_performance_snapshots` | Score, trend, status |
| Performance trends | `wallet_performance_trends` | Score drift |
| Performance actions | `wallet_performance_actions` | Promotions, demotions, retirements |
| Strategy profiles | `strategy_profiles` | Archetype, confidence |
| Trader registry | `wallets`, `wallet_reports` | Classification, watch_score |
| Wallet lifecycle | `wallet_lifecycle` | State, rank |
| Acquisition records | `wallet_acquisition_records` | Quality, source |

### Existing Metrics (Reusable)

| Module | Metrics |
|---|---|
| `trader_alpha.py` | Activity-based alpha_score |
| `wallet_performance.py` | ROI, Sharpe-like, expectancy, drawdown |
| `wallet_signal_analytics.py` | Per-wallet win rate, archetype performance |
| `wallet_scoring.py` | Composite wallet score |
| `paper_copy_trader` | Realized paper outcomes |

### Current Gaps

- No unified **WalletAlphaReport** combining paper outcomes + signal validation + performance
- No **baseline comparison** framework (promoted vs average vs random)
- No **signal decay** measurement (half-life, persistence)
- No **promotion validation** with quantitative evidence
- No **AlphaScore** grade system for research ranking

### Alpha Measurement Opportunities

1. Excess return vs average discovered wallet (paper copy ROI)
2. Risk-adjusted return vs paper trading baseline
3. Signal accuracy vs random (50%) baseline
4. Promotion cohort outperformance
5. Archetype-level alpha decomposition
6. Score drift / decay over time

## Architecture

```
wallet_alpha_lab.py          → WalletAlphaReport, AlphaScore, archetype + promotion analysis
wallet_baseline_analysis.py  → baseline comparisons
wallet_signal_decay.py       → DecayReport, half-life, persistence
        │
        ▼
WalletAutonomyService alpha cycle (24h)
        │
        ▼
Dashboard Alpha Lab page + CLI
```

## Out of Scope

Live trading, execution, credentials, new trading engine.

## Critical Validation Questions

1. Are promoted wallets outperforming average wallets?
2. Are discovered wallets generating useful signals?
3. Which archetypes produce the most alpha?
4. How quickly do wallet signals decay?
5. Is the wallet intelligence system improving over time?

## Success Criteria

Quantifiable alpha measurements, baseline framework, decay analysis, dashboard, autonomy integration, full tests.
