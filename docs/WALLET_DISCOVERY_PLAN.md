# Wallet Discovery Engine — Integration Plan

Branch: `feature/wallet-discovery-engine`

## Objective

Build an autonomous Wallet Discovery Engine that continuously finds, ranks, validates, and maintains high-signal traders by extending existing Polylens discovery, scoring, and intelligence infrastructure — without duplicate systems.

## Existing Capabilities Audit

### Wallet Discovery Code

| Module | Path | Capabilities |
|---|---|---|
| Trader discovery | `src/analysis/trader_discovery.py` | Counterparty + co-market discovery, `discovered_wallets`, `discovery_relationships` |
| Trader scanner | `src/analysis/trader_scanner.py` | `discover_wallets()`, `scan_wallets()`, watchlist/registry sources |
| Wallet tracker | `src/intelligence/wallet_tracker.py` | Ranked watchlist, composite scoring, paper-copy sync |
| Wallet forensics | `src/analysis/wallet_forensics.py` | Classification, metrics, behavioral signals |
| Trader registry | `src/analysis/trader_registry.py` | `wallets`, `wallet_reports`, `calculate_watch_score()` |
| Trader alpha | `src/analysis/trader_alpha.py` | `calculate_alpha_score()`, `rank_trader_alpha()` |
| Strategy classifier | `src/intelligence/strategy_classifier.py` | Archetypes, `strategy_profiles` |
| Signal analytics | `src/intelligence/wallet_signal_analytics.py` | Win rate, ROI, archetype performance |

### Existing Databases

| Database | Tables | Purpose |
|---|---|---|
| `data/trader_discovery.db` | `discovered_wallets`, `discovery_relationships` | Discovery candidates |
| `data/traders.db` | `wallets`, `wallet_reports`, `wallet_watchlist`, `strategy_profiles` | Registry + intelligence |
| `data/wallet_activity.db` | `wallet_exports`, `wallet_events` | Activity history |
| `data/trader_signals.db` | signals, recommendations, validation | Signal pipeline |
| `data/paper_copy_trader.db` | `watched_traders`, `paper_copy_positions` | Paper copy outcomes |

### Existing CLI

- `discover-traders` — counterparty/co-market discovery
- `scan-top-traders` — scan watchlist/registry wallets
- `analyze-trader` — single-wallet forensics + registry
- `wallet-signal-integration-cycle` — signal + paper integration

### Existing Dashboard

Trader Intelligence Center: Overview, Network, Profiles, Signals, Insights (`src/web/trader_dashboard.py`)

## Component Classification

| Proposed Component | Classification | Approach |
|---|---|---|
| Wallet Discovery Engine | **Extend Existing** | Orchestrate `trader_discovery`, `trader_scanner`, `WalletTracker` |
| Wallet Scoring | **Extend Existing** | Unify `calculate_watch_score`, `calculate_alpha_score`, paper/signal metrics |
| Lifecycle Management | **New Component Needed** | `wallet_lifecycle` + events tables in `traders.db` |
| Discovery Database | **Extend Existing** | Add `discovery_runs`, `wallet_score_history`, `wallet_lifecycle_events` |
| Dashboard | **Extend Existing** | New Discovery page on trader dashboard |
| Analytics | **Extend Existing** | `wallet_discovery_analytics.py` cross-DB reports |

## Architecture

```
WalletDiscoveryEngine (src/intelligence/wallet_discovery.py)
    ├── trader_discovery.discover_trader_candidates()
    ├── trader_scanner.discover_wallets() / scan_wallets()
    ├── WalletTracker.refresh_wallets()
    ├── WalletScorer.score_wallet() / rank_wallets()
    └── lifecycle evaluate → promote/demote → persist

WalletScorer (src/intelligence/wallet_scoring.py)
    ├── trader_registry + trader_alpha
    ├── wallet_signal_analytics (ROI, win rate)
    ├── wallet_activity (freshness)
    └── strategy_classifier (category/archetype)

Downstream (unchanged):
    WalletTracker.watchlist → SignalEngine → paper trading
```

## Out of Scope

- New trading engine, execution framework, portfolio system, signal framework
- Live trading, API keys, hardcoded wallet lists

## Success Criteria

- Integrated with Wallet Intelligence + Wallet Signal layers
- Dashboard visibility, analytics, CLI, tests, documentation
- No conflicting storage architecture
