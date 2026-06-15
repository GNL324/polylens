# Wallet Signal Integration Plan

Branch: `feature/wallet-signal-integration`

## Objective

Integrate the Wallet Intelligence Layer (`src/intelligence/`) as a first-class signal source in existing Polylens trader signal, recommendation, analytics, and paper-trading workflows — without creating parallel systems.

## Existing Trader Signal Flow (Audit)

```
Wallet activity exports (data/wallets/*_activity.json)
    │
    ▼  wallet_activity.py — export_wallet_activity(), save_wallet_activity_export()
wallet_activity.db + JSON exports
    │
    ▼  trader_signal_engine.py — load_activity_json(), generate_signals_from_activity()
Raw signals: early_entry, conviction, exit, consensus, contrarian
    │
    ▼  intelligence/signal_engine.py — filter_signals() (staleness, liquidity, dedup)
Filtered signals
    │
    ▼  trader_signal_engine.py — persist_signals(), score_trader_signals()
trader_signals.db → trader_signals
    │
    ▼  trader_signal_engine.py — generate_trader_signal_recommendations()
    │  trader_signal_gates.py — apply_gate_to_recommendation()
trader_signal_recommendations (paper_entry, paper_exit, watch, avoid, blocked)
    │
    ▼  trader_signal_paper_bridge.py — run_trader_signal_paper_bridge()
trader_signal_paper_intents (blocked, candidate, simulated)
    │
    ├─► opportunity_feed.py — get_paper_trading_opportunities()  [INTEGRATION TARGET]
    │       ▼
    │   paper_trading_engine.py — run_paper_trading_engine() (Kelly sizing, capital allocation)
    │
    └─► paper_copy_trader.py — run_paper_copy_trader()  [INTEGRATION TARGET]
            watched_traders → mirror buy/sell events
```

### Parallel Intelligence Layer (Already on `main`)

| Module | Role |
|---|---|
| `WalletTracker` | Discover, refresh, score, rank wallets; `wallet_watchlist` in `traders.db` |
| `StrategyClassifier` | Archetype mapping; `strategy_profiles` in `traders.db` |
| `SignalEngine` | Wallet-watch signal cycle + paper bridge |
| `run_wallet_intelligence_cycle()` | End-to-end intelligence orchestration |

### Existing Systems to Reuse (Do Not Rewrite)

| System | Module | Role for wallet signals |
|---|---|---|
| Trader signal engine | `trader_signal_engine.py` | Signal generation, scoring, recommendations |
| Signal gates | `trader_signal_gates.py` | Statistical promotion gates |
| Signal validation | `trader_signal_validation.py` | Accuracy feedback |
| Paper bridge | `trader_signal_paper_bridge.py` | Recommendation → paper intent |
| Strategy recommendations | `strategy_recommendations.py` | Short-crypto strategy trust (separate domain) |
| Opportunity feed | `opportunity_feed.py` | Paper trading opportunity aggregation |
| Paper trading engine | `paper_trading_engine.py` | Capital allocation, position sizing |
| Paper copy trader | `paper_copy_trader.py` | Wallet mirror paper trading |
| Paper analytics | `paper_analytics.py` | Performance reporting |
| Dashboard views | `trader_signal_dashboard_views.py` | Grafana/SQL views |
| Trader dashboard | `web/trader_dashboard.py` | Trader Intelligence Center UI |

**Note:** `strategy_recommendations.py` serves short-crypto paper strategies. Wallet-derived recommendations flow through `trader_signal_recommendations` — the wallet-specific recommendation engine.

## Integration Gaps (Pre-Implementation)

| Gap | Impact |
|---|---|
| Simulated paper intents not in `opportunity_feed` | Paper trading engine never sees wallet signals |
| Watchlist not synced to `paper_copy_trader.watched_traders` | Copy trader runs independently |
| Strategy archetypes not linked to trader profiles | Profiles page lacks archetype context |
| No unified wallet analytics | Win rate, ROI, archetype performance not aggregated |
| Intelligence layer not in CLI or dashboard | No operational visibility |
| `src/intelligence` not connected to registry refresh | Watchlist scores not propagated |

## Implementation Plan

### Phase 2 — Connect Intelligence to Existing Systems

| Connection | Implementation |
|---|---|
| WalletTracker → trader registry | `sync_watchlist_to_registry()` updates rank metadata; watchlist sourced from registry |
| StrategyClassifier → trader profiles | `link_profile_to_registry()` annotates wallet reports with archetype |
| SignalEngine → recommendation engine | Existing `run_trader_signal_cycle()` + `generate_trader_signal_recommendations()` |

### Phase 3 — Emit Wallet Recommendations into Paper Trading

| Step | Implementation |
|---|---|
| Feed simulated intents | `opportunity_feed._trader_signal_intent_rows()` |
| Sync watchlist to copy trader | `WalletTracker.sync_watchlist_to_paper_copy()` |
| Integration cycle | `run_wallet_signal_integration_cycle()` chains intelligence + paper paths |

### Phase 4 — Analytics

New module: `src/intelligence/wallet_signal_analytics.py`

- Wallet win rate (paper copy + signal validation)
- Wallet ROI (paper copy closed positions)
- Strategy archetype performance (`strategy_profiles` × paper outcomes)
- Top copied wallets leaderboard

### Phase 5 — Dashboard Visibility

- Add **Signals** page to `trader_dashboard.py`
- Load KPIs from `trader_signal_dashboard_views` + `wallet_signal_analytics`
- Show pipeline status, top copied wallets, archetype performance

### Phase 6 — Tests and Validation

- Unit tests for feed integration, analytics, integration cycle
- `reports/wallet_signal_integration_validation_report.md`
- Full test suite must pass

## Architecture After Integration

```
src/intelligence/
├── wallet_tracker.py          ──► traders.db, wallet_activity.db, paper_copy_trader.db
├── strategy_classifier.py     ──► traders.db (strategy_profiles + registry annotation)
├── signal_engine.py           ──► trader_signals.db
├── wallet_signal_integration.py  ──► orchestrates full integration cycle
└── wallet_signal_analytics.py    ──► cross-DB analytics reports

trader_signal_paper_intents (simulated)
    └──► opportunity_feed ──► paper_trading_engine

wallet_watchlist (top wallets)
    └──► paper_copy_trader.watched_traders ──► run_paper_copy_trader()
```

## Out of Scope

- New trading engine or execution path
- Live trading, API keys, exchange credentials
- Modifications to adapters, risk engine, Kalshi execution framework

## Success Criteria

- Wallet intelligence is a first-class signal source in opportunity feed and paper trading
- Recommendations flow through existing `trader_signal_recommendations` pipeline
- Analytics and dashboard expose wallet signal performance
- All existing tests pass; new integration tests added
- No duplicate systems created
