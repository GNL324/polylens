# Wallet Autonomy Service — Integration Plan

Branch: `feature/wallet-autonomy-service`

## Objective

Transform the Wallet Intelligence System into a continuously operating autonomous service that orchestrates discovery, scoring, performance evaluation, feedback, and analytics — paper-only, read-only, with no execution-path changes.

## Existing Infrastructure Audit

### Services

| Component | Path | Pattern |
|---|---|---|
| Paper trading service | `src/analysis/paper_trading_service.py` | oneshot cycle + health + structured logging |
| Trader signal cycle | `src/cli.py` `trader-signal-cycle` | CLI entry for systemd timer |
| Wallet intelligence cycle | `src/intelligence/signal_engine.py` | `run_wallet_intelligence_cycle()` |
| Wallet signal integration | `src/intelligence/wallet_signal_integration.py` | full signal → paper bridge cycle |
| Wallet discovery cycle | `src/intelligence/wallet_discovery.py` | `run_discovery_cycle()` |
| Wallet feedback cycle | `src/intelligence/wallet_feedback_engine.py` | `run_wallet_feedback_cycle()` |

### Systemd / Timers

| Unit | Interval | Command |
|---|---|---|
| `polylens-paper-trading.timer` | 5 min | `paper-trading-service` |
| `polylens-trader-signal-cycle.timer` | 15 min | `trader-signal-cycle` |
| `polylens-short-crypto-paper.timer` | configurable | paper-only CLI |

**Pattern:** `Type=oneshot` service + `Persistent=true` timer, `POLYLENS_LIVE_TRADING=false`, user-level install (no root required for user timers).

### CLI Commands (wallet intelligence)

| Command | Role |
|---|---|
| `wallet-discover` / `wallet-rank` | Discovery and ranking |
| `wallet-signal-integration-cycle` | Signal refresh + paper bridge |
| `wallet-performance` / `wallet-feedback-cycle` | Performance and feedback |
| `wallet-performance-report` | Analytics |

### Dashboard

| Page | Data loader |
|---|---|
| Discovery | `load_wallet_discovery_dashboard()` |
| Performance | `load_wallet_performance_dashboard()` |
| Signals | `load_wallet_signal_dashboard()` |

Refresh: NiceGUI `ui.timer` on mission control; trader dashboard manual refresh buttons.

## Reusable Components

| Module | Reuse |
|---|---|
| `WalletDiscoveryEngine` | Discovery cycle |
| `WalletTracker` | Watchlist refresh in signal cycle |
| `WalletScorer` | Scoring in discovery cycle |
| `WalletPerformanceEngine` | Performance cycle |
| `WalletFeedbackEngine` | Feedback cycle |
| `run_wallet_signal_integration_cycle` | Signal cycle |
| Analytics modules | Analytics cycle |
| `paper_trading_service` health pattern | Health monitoring model |
| `traders.db` SQLite pattern | State persistence |

## Architecture

```
wallet-autonomy.timer (5 min)
        │
        ▼
wallet-service-run (CLI)
        │
        ▼
WalletAutonomyService
        │
        ├── discovery cycle (6h) ──► WalletDiscoveryEngine.run_discovery_cycle()
        ├── signals cycle (5m) ────► run_wallet_signal_integration_cycle()
        ├── performance cycle (1h) ► WalletPerformanceEngine
        ├── feedback cycle (1h) ───► WalletFeedbackEngine
        └── analytics cycle (6h) ──► analytics reports + wallet_autonomy_report

State: wallet_service_state, wallet_service_cycle_runs (traders.db)
Health: wallet_service_health.py
Dashboard: Service page (read-only)
```

## Scheduling Model

Single timer fires every 5 minutes. Service checks `wallet_service_state.last_run_at` per cycle and runs only due cycles. Intervals:

| Cycle | Interval |
|---|---|
| discovery | 6 hours |
| signals | 5 minutes |
| performance | 1 hour |
| feedback | 1 hour |
| analytics | 6 hours |

## Missing Components (to build)

1. `WalletAutonomyService` orchestrator
2. `wallet_service_state` persistence
3. `wallet_service_health.py`
4. Service dashboard page
5. CLI commands (`wallet-service-*`)
6. `wallet-autonomy.service` / `.timer`
7. `wallet_autonomy_report` with history
8. Tests and validation report

## Out of Scope

- Live trading, order placement, exchange integrations, credentials
- New execution frameworks or duplicate intelligence modules
- Kalshi/Polymarket execution changes

## Success Criteria

Paper-only autonomous wallet lifecycle with dashboard visibility, systemd deployment, health monitoring, full tests, and documentation.
