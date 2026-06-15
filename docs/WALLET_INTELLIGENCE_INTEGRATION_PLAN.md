# Wallet Intelligence Layer — Integration Plan

Branch: `feature/wallet-intelligence-layer`

This document audits the Polylens repository and classifies each proposed Wallet Intelligence component before implementation.

## Repository Audit Summary

### Existing Wallet Analysis Capabilities

| Capability | Location | Description |
|---|---|---|
| Activity ingestion | `src/analysis/wallet_activity.py` | Polymarket activity fetch, normalization, SQLite persistence (`wallet_activity.db`) |
| Forensics & classification | `src/analysis/wallet_forensics.py` | Metrics, signal detection, wallet archetype scoring |
| Wallet scanning | `src/analysis/trader_scanner.py` | Full scan pipeline: export → forensics → registry |
| Trader registry | `src/analysis/trader_registry.py` | Persistent wallet profiles, watch scores (`traders.db`) |
| Discovery | `src/analysis/trader_discovery.py` | Co-market participant discovery (`trader_discovery.db`) |
| Alpha scoring | `src/analysis/trader_alpha.py` | Composite alpha score from registry + discovery |
| DNA similarity | `src/analysis/trader_dna.py` | 17-dim feature vectors, clustering |
| Families | `src/analysis/trader_families.py` | Union-find trader family detection |
| Profiler | `src/analysis/trader_profiler.py` | Human-readable specialization labels |
| Replay | `src/analysis/trader_replay.py` | Timeline and pattern analysis |
| Network | `src/analysis/trader_network.py` | Trader relationship graphs |
| Insights | `src/analysis/trader_insights.py` | Recommended traders, relationships |
| Reports | `src/reports/wallet_report.py` | Wallet report generation |

### Existing Trader Classification

| Capability | Location | Description |
|---|---|---|
| Forensics classes | `wallet_forensics.py` | `market_maker`, `arbitrage_trader`, `quantitative_directional`, `mixed`, `unknown` |
| Specialization | `trader_profiler.py` | BTC/ETH/SOL specialist, market maker, arb, directional, etc. |
| Alpha classification | `trader_alpha.py` | Alpha score with class-weighted modifiers |
| Families | `trader_families.py` | Cluster-based family typing |

### Existing Strategy Analytics

| Capability | Location | Description |
|---|---|---|
| Strategy feedback | `strategy_feedback.py` | Paper trade ROI feedback by strategy label |
| Strategy recommendations | `strategy_recommendations.py` | Trust increase/decrease/hold from feedback |
| Trader signal engine | `trader_signal_engine.py` | Signal generation, scoring, recommendations |
| Signal validation | `trader_signal_validation.py` | Accuracy tracking, rolling windows |
| Signal gates | `trader_signal_gates.py` | Statistical promotion gates |

### Existing Signal Generation

| Capability | Location | Description |
|---|---|---|
| Trader signals | `trader_signal_engine.py` | `early_entry`, `conviction`, `exit`, `consensus`, `contrarian` |
| Arb signals | `arb_signals.py` | Arbitrage-specific signal detection |
| Signal paper bridge | `trader_signal_paper_bridge.py` | Recommendation → paper intent conversion |

### Existing Paper Trading

| Capability | Location | Description |
|---|---|---|
| Paper engine | `paper_trading_engine.py` | Strategy-based simulation with Kelly sizing |
| Paper service | `paper_trading_service.py` | Service wrapper |
| Paper copy trader | `paper_copy_trader.py` | Wallet mirror trading |
| Paper analytics | `paper_analytics.py` | Performance analytics |
| Paper settlement | `paper_settlement.py` | Position settlement |
| Signal paper bridge | `trader_signal_paper_bridge.py` | Intent generation from proven signals |

### Existing Risk Controls

| Capability | Location | Description |
|---|---|---|
| Risk engine | `src/risk/engine.py` | Persistent halt/reject/paper_only decisions |
| Trading risk | `src/trading/risk.py` | Stateless Kalshi order checks |
| Signal gates | `trader_signal_gates.py` | Statistical signal-family gates |
| Web risk | `src/web/risk.py` | Dashboard risk views |

### Existing Execution Systems

| Capability | Location | Description |
|---|---|---|
| Kalshi executor | `src/trading/executor.py` | Paper-only Kalshi order submission |
| Paper portfolio | `src/trading/paper.py` | Order journaling |
| Short crypto executor | `short_crypto_executor.py` | Short-crypto execution (kill switch) |

**Note:** No live trading or order placement is added by this integration.

### Existing Databases

| Database | Tables | Purpose |
|---|---|---|
| `data/wallet_activity.db` | `wallet_exports`, `wallet_events` | Activity history |
| `data/traders.db` | `wallets`, `wallet_reports` | Registry and forensics reports |
| `data/trader_discovery.db` | `discovered_wallets`, `discovery_relationships` | Discovery graph |
| `data/trader_signals.db` | `trader_signals`, `signal_performance`, `trader_signal_recommendations`, `trader_signal_validation`, `trader_signal_paper_intents`, `trader_signal_cycles` | Signal pipeline |
| `data/paper_trading.db` | `paper_runs`, `paper_orders`, `paper_positions`, etc. | Strategy paper trading |
| `data/paper_copy_trader.db` | `watched_traders`, `paper_copy_positions`, etc. | Wallet mirror trading |
| `data/polylens.db` | `opportunities`, `risk_events`, `position_exposure`, etc. | Opportunities + risk |

### Existing Dashboards

| Dashboard | Location | Description |
|---|---|---|
| Trader Intelligence Center | `src/web/trader_dashboard.py` | Alpha leaderboard, profiles, network, insights |
| Signal dashboard views | `trader_signal_dashboard_views.py` | Signal-specific Grafana/dashboard views |
| Mission control | `src/web/mission_control.py` | System overview |
| Root dashboard | `src/dashboard.py` | CLI dashboard aggregator |

---

## Component Classification

### Wallet Intelligence Layer (`src/intelligence/`)

| Component | Classification | Rationale |
|---|---|---|
| `wallet_tracker.py` | **Extend Existing** | Wraps `wallet_activity`, `trader_registry`, `trader_discovery`, `trader_scanner`, `paper_copy_trader` watchlists |
| `strategy_classifier.py` | **Extend Existing** | Maps `wallet_forensics` + `trader_profiler` + `trader_alpha` to signal-oriented archetypes |
| `signal_engine.py` | **Extend Existing** | Orchestrates `trader_signal_engine` + `trader_signal_paper_bridge` with wallet-specific filters |

### Proposed Components (Gap Analysis Targets)

| Component | Classification | Rationale |
|---|---|---|
| **WalletTracker** | **Extend Existing** | Core exists in `wallet_activity` + `trader_registry` + `trader_scanner`; thin orchestration layer needed |
| **StrategyClassifier** | **Extend Existing** | Forensics/profiler exist; new archetype mapping (`EARLY_MOVER`, etc.) is additive |
| **SignalEngine** | **Extend Existing** | `trader_signal_engine` is mature; needs wallet-watch integration and dedup/staleness filters |
| **RiskGate** | **Already Exists** | `risk/engine.py` + `trader_signal_gates.py` — no new module needed |
| **PositionManager** | **Already Exists** | `paper_trading_engine.py` + `paper_copy_trader.py` — no parallel manager |
| **AgentOrchestrator** | **Not Needed** | Systemd timers + CLI already orchestrate cycles; intelligence layer exposes callable cycle, not a new scheduler |

### Integration Targets (Do Not Rewrite)

| System | Classification | Notes |
|---|---|---|
| Adapters | **Already Exists** | Reuse `polymarket.py` activity API |
| Paper trading engine | **Already Exists** | Signals flow via `trader_signal_paper_bridge` |
| Capital allocation | **Already Exists** | `paper_trading_engine` Kelly sizing unchanged |
| Strategy recommendations | **Already Exists** | `strategy_recommendations.py` + trader signal recommendations |
| Feedback loops | **Already Exists** | `strategy_feedback.py` + `trader_signal_validation.py` |
| Dashboard services | **Already Exists** | Existing dashboards consume registry/signal data |
| Opportunity storage | **Already Exists** | `opportunity_store.py` unchanged |
| Risk controls | **Already Exists** | `risk/engine.py` unchanged |
| Kalshi execution | **Already Exists** | No modifications |

---

## Integration Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                    src/intelligence/ (NEW)                       │
│  wallet_tracker → strategy_classifier → signal_engine            │
└────────────┬──────────────────┬──────────────────┬────────────┘
             │                  │                  │
             ▼                  ▼                  ▼
   wallet_activity.db    traders.db         trader_signals.db
   trader_registry       wallet_forensics    trader_signal_engine
   trader_discovery      trader_profiler     trader_signal_gates
   paper_copy_trader     trader_alpha        trader_signal_paper_bridge
                                              paper_copy_trader
                                              strategy_recommendations
```

All wallet-derived signals enter the existing recommendation and paper-trading workflows. No parallel execution path is created.

---

## Implementation Scope

### Build (New)

- `src/intelligence/wallet_tracker.py`
- `src/intelligence/strategy_classifier.py`
- `src/intelligence/signal_engine.py`
- `src/intelligence/__init__.py`
- Tests and documentation

### Reuse (No Changes Required)

- All adapters, paper engines, risk controls, dashboards, opportunity storage, Kalshi framework

### Explicitly Out of Scope

- Live trading, API keys, private keys, order placement
- Parallel trading engine or duplicate storage architecture
- New RiskGate, PositionManager, or AgentOrchestrator modules

---

## Success Criteria Mapping

| Criterion | Approach |
|---|---|
| No duplicate systems | Intelligence layer is a thin facade over existing modules |
| No parallel trading engine | Signals → `trader_signal_paper_bridge` → existing paper paths |
| No live execution | All outputs flagged `read_only=True`, `paper_only=True` |
| No breaking changes | New package only; existing modules untouched |
| Tests passing | Unit tests for each intelligence module |
| Documentation complete | This plan + gap analysis + validation report |
