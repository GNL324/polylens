# Wallet Performance Engine — Integration Plan

Branch: `feature/wallet-performance-engine`

## Objective

Build a Wallet Performance Engine that continuously measures, scores, promotes, demotes, and retires wallets based on actual signal performance — extending existing Polylens intelligence infrastructure without duplicate systems.

## Existing Capabilities Audit

### Wallet Discovery (`src/intelligence/wallet_discovery.py`)

| Capability | Status |
|---|---|
| Multi-source discovery | Exists |
| `WalletScorer` integration | Exists |
| Lifecycle states (active/watchlist/probation/retired) | Exists in `wallet_lifecycle` |
| `wallet_score_history`, `wallet_lifecycle_events` | Exists in `traders.db` |
| Promotion/demotion via `apply_lifecycle()` | Exists — rule-based on composite score |

**Gap:** Lifecycle uses composite discovery score, not signal-performance-specific metrics (ROI, expectancy, Sharpe, signal decay).

### Wallet Scoring (`src/intelligence/wallet_scoring.py`)

| Capability | Status |
|---|---|
| `WalletScore` with ROI, expectancy, win rate, freshness | Exists |
| Paper copy + signal validation inputs | Exists |
| Rank wallets | Exists |

**Gap:** No trend, survival duration, Sharpe-like metric, or performance-specific status (promoted/active/probation/retired).

### Wallet Analytics (`src/intelligence/wallet_signal_analytics.py`, `wallet_discovery_analytics.py`)

| Capability | Status |
|---|---|
| Per-wallet win rate, ROI, signal accuracy | Exists |
| Archetype performance | Exists |
| Discovery rate, survival rate | Exists in discovery analytics |

**Gap:** No score drift, signal decay, promoted vs retired cohort analysis.

### Paper Copy Trader (`src/analysis/paper_copy_trader.py`)

| Capability | Status |
|---|---|
| `paper_copy_report()` with per-wallet ROI, win rate, PnL | Exists |
| Position-level closed trade data | Exists in `paper_copy_positions` |

### Trader Signal Engine (`src/analysis/trader_signal_engine.py`)

| Capability | Status |
|---|---|
| Signal generation, scoring, recommendations | Exists |
| `signal_performance` table | Exists |

### Signal Validation (`src/analysis/trader_signal_validation.py`)

| Capability | Status |
|---|---|
| Per-trader accuracy, roi_proxy | Exists — used by analytics |

### Strategy Recommendations

| System | Role |
|---|---|
| `trader_signal_recommendations` | Wallet signal recommendations |
| `strategy_recommendations.py` | Short-crypto strategy trust (separate) |

## Component Classification

| Proposed Component | Classification | Approach |
|---|---|---|
| Performance database | **Extend Existing** | Add `wallet_performance_*` tables to `traders.db` |
| `WalletPerformanceScore` | **New Component Needed** | Richer score object with trend + status |
| Feedback loop engine | **Extend Existing** | Configurable rules over discovery lifecycle |
| Performance analytics | **Extend Existing** | New module building on signal/discovery analytics |
| Discovery feedback | **Extend Existing** | Boost/penalize rankings in discovery + tracker |
| Dashboard | **Extend Existing** | New Performance page |
| CLI | **Extend Existing** | New commands + extend promotions |

## Architecture

```
wallet_performance.py
    ├── WalletPerformanceScore (ROI, Sharpe, trend, status)
    ├── snapshots + action history persistence
    └── integrates paper_copy + signal_validation + registry

wallet_feedback_engine.py
    ├── configurable promote/demote/retire/reactivate rules
    ├── writes wallet_lifecycle (reuse discovery table)
    └── logs wallet_performance_actions

wallet_performance_analytics.py
    └── distributions, drift, decay, cohort analysis

Downstream integration:
    WalletScorer ← performance boost
    WalletTracker ← performance-weighted composite
    WalletDiscovery ← prioritize successful wallets
```

## Out of Scope

- New trading engine, execution framework, signal framework, portfolio system
- Live trading, API keys, credentials

## Success Criteria

Integrated with Discovery, Intelligence, Signal, and Analytics layers with dashboard visibility, tests, and documentation.
