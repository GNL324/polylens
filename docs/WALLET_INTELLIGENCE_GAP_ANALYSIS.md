# Wallet Intelligence Layer — Gap Analysis

Branch: `feature/wallet-intelligence-layer`

Evaluation of six proposed components against the existing Polylens codebase.

---

## 1. WalletTracker

### Existing Implementation Found

| Module | Capability |
|---|---|
| `wallet_activity.py` | `export_wallet_activity()`, `save_wallet_activity_export()`, `wallet_events` table with `event_key` dedup |
| `trader_registry.py` | `list_traders()`, `top_traders()`, `calculate_watch_score()`, `save_wallet_report()` |
| `trader_discovery.py` | `load_discovered_wallets()`, `discover_from_registry()` |
| `trader_scanner.py` | `discover_wallets()`, `scan_wallet()`, `ManualWalletSource`, `RegistryWalletSource` |
| `paper_copy_trader.py` | `load_watched_wallets()`, `watch_trader()` |
| `trader_alpha.py` | `rank_trader_alpha()`, `build_trader_alpha_report()` |

### Missing Functionality

- Unified API to discover, refresh, score, and rank wallets across all sources (watchlist, registry, discovery, paper-copy watched)
- Incremental refresh helper that queries latest `wallet_events.timestamp` before re-export
- Ranked watchlist persistence with composite score (watch_score + alpha_score + discovery_score)
- Single entry point for "top wallets to monitor"

### Recommended Integration Approach

Implement `src/intelligence/wallet_tracker.py` as a facade that:

1. Aggregates wallets from existing sources via `discover_wallets()` + `load_watched_wallets()` + `load_discovered_wallets()`
2. Calls `export_wallet_activity()` for incremental refresh (with optional `limit`)
3. Scores via `calculate_watch_score()` and `build_trader_alpha_report()`
4. Persists watchlist rankings to `traders.db` (`wallet_watchlist` table, following existing SQLite conventions)

### Estimated Implementation Effort

**Small (1–2 days)** — mostly wiring; no new storage architecture.

---

## 2. StrategyClassifier

### Existing Implementation Found

| Module | Capability |
|---|---|
| `wallet_forensics.py` | `classify_wallet()` → market_maker, arbitrage_trader, quantitative_directional, mixed, unknown |
| `trader_profiler.py` | `derive_specialization()`, `TraderProfile` |
| `trader_alpha.py` | `TraderAlphaReport`, `calculate_alpha_score()` |
| `trader_dna.py` | Behavioral feature vectors |
| `trader_families.py` | Cluster family typing |

### Missing Functionality

- Signal-oriented archetypes: `EARLY_MOVER`, `CONTRA_FADE`, `NEWS_TRADER`, `ARB_HUNTER`, `SIZE_SCALPER`, `CONVICTION_HOLD`, `MOMENTUM_RIDER`
- `StrategyProfile` dataclass with archetype, confidence, supporting metrics
- Persistence of strategy profiles alongside wallet reports

### Recommended Integration Approach

Implement `src/intelligence/strategy_classifier.py` that:

1. Loads forensics report from `trader_registry.load_wallet_report()` or runs `build_wallet_forensics_report()`
2. Maps existing metrics + forensics classification to archetypes via rule-based scoring
3. Emits `StrategyProfile` objects
4. Persists to `traders.db` (`strategy_profiles` table)

Does **not** replace `wallet_forensics.classify_wallet()` — extends it with trading-style archetypes.

### Estimated Implementation Effort

**Small–Medium (2–3 days)** — rule engine + persistence + tests.

---

## 3. SignalEngine

### Existing Implementation Found

| Module | Capability |
|---|---|
| `trader_signal_engine.py` | Full pipeline: `generate_signals_from_activity()`, `persist_signals()`, `score_trader_signals()`, `generate_trader_signal_recommendations()`, `run_trader_signal_cycle()` |
| `trader_signal_gates.py` | Statistical promotion gates |
| `trader_signal_validation.py` | Accuracy feedback |
| `trader_signal_paper_bridge.py` | Recommendation → paper intent |

### Missing Functionality

- Wallet-watch-driven signal cycle (auto-load activity exports for watched wallets)
- Staleness rejection (signals older than configurable threshold)
- Liquidity validation (minimum amount/shares)
- Duplicate position avoidance (same wallet+market+side already signaled or in paper copy)
- Unified cycle that chains signal generation → recommendations → paper bridge

### Recommended Integration Approach

Implement `src/intelligence/signal_engine.py` that:

1. Loads watched wallet activity from `wallet_tracker` exports
2. Delegates signal generation to `trader_signal_engine` functions
3. Applies wallet-intelligence filters (staleness, liquidity, dedup) before persistence
4. Calls `run_trader_signal_cycle()` and `run_trader_signal_paper_bridge()` for integration
5. Returns cycle summary with `read_only=True`, `paper_only=True` flags

### Estimated Implementation Effort

**Medium (3–4 days)** — filter logic + multi-wallet batch + integration tests.

---

## 4. RiskGate

### Existing Implementation Found

| Module | Capability |
|---|---|
| `risk/engine.py` | `RiskEngine.evaluate()` — halt, reject, paper_only with exposure limits |
| `trading/risk.py` | Stateless order-level checks |
| `trader_signal_gates.py` | Signal-family statistical gates (`proven/unproven/weak/blocked`) |

### Missing Functionality

**None significant.** Risk gating is fully implemented at both position and signal levels.

### Recommended Integration Approach

**Do not create a new module.** `signal_engine.py` calls existing gates via `trader_signal_gates.apply_gate_to_recommendation()` and paper bridge eligibility checks. Position-level risk remains in `risk/engine.py`.

### Estimated Implementation Effort

**None** — reuse only.

---

## 5. PositionManager

### Existing Implementation Found

| Module | Capability |
|---|---|
| `paper_trading_engine.py` | Strategy-based positions with Kelly sizing, exposure caps |
| `paper_copy_trader.py` | Wallet-mirror positions, open/close lifecycle |
| `trader_signal_paper_bridge.py` | Paper intents (`blocked/candidate/simulated`) |

### Missing Functionality

- Consumer that reads `simulated` intents and places paper positions automatically

This is a known gap in the existing pipeline but **creating a parallel PositionManager would violate the no-duplicate-systems rule**.

### Recommended Integration Approach

**Do not create a new PositionManager.** Wallet signals flow into `trader_signal_paper_intents` via the existing paper bridge. Future work can wire `simulated` intents into `paper_copy_trader` without a new abstraction layer.

### Estimated Implementation Effort

**Deferred** — out of scope for this branch to avoid duplicate systems.

---

## 6. AgentOrchestrator

### Existing Implementation Found

| Module | Capability |
|---|---|
| `trader_signal_engine.py` | `run_trader_signal_cycle()` |
| `trader_signal_paper_bridge.py` | `run_trader_signal_paper_bridge()` |
| `deploy/systemd/polylens-trader-signal-cycle.timer` | Scheduled signal cycle |
| `cli.py` | Manual invocation of all pipelines |

### Missing Functionality

- End-to-end wallet intelligence cycle combining tracker → classifier → signal engine

### Recommended Integration Approach

**Do not create AgentOrchestrator.** Instead, expose `run_wallet_intelligence_cycle()` in `signal_engine.py` that chains the three intelligence modules. Scheduling remains via existing systemd timers and CLI — no new orchestration framework.

### Estimated Implementation Effort

**Small (within signal_engine)** — single callable cycle function.

---

## Summary Matrix

| Component | Status | Action | Effort |
|---|---|---|---|
| WalletTracker | Partial | **Extend** → `wallet_tracker.py` | Small |
| StrategyClassifier | Partial | **Extend** → `strategy_classifier.py` | Small–Medium |
| SignalEngine | Partial | **Extend** → `signal_engine.py` | Medium |
| RiskGate | Complete | **Reuse** existing | None |
| PositionManager | Complete | **Reuse** existing | None |
| AgentOrchestrator | N/A | **Not Needed** — use cycle function + existing schedulers | Small |

---

## Priority Order

1. `wallet_tracker.py` — foundation for watched wallet set
2. `strategy_classifier.py` — archetype profiles for signal weighting
3. `signal_engine.py` — signal generation + paper bridge integration
4. Tests for all three modules
5. Validation report
