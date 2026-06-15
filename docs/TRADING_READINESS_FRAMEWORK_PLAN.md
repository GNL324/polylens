# Trading Readiness Framework — Plan

Branch: `feature/trading-readiness-framework`

## Objective

Prepare Polylens for future live execution with blocked-by-default gates, human approval, readiness checks, kill switch, and simulated routing — without enabling live trading on this branch.

## Execution Audit

### Kalshi (`src/trading/executor.py`, `src/trading/risk.py`)

| Component | Status |
|---|---|
| `KalshiExecutor.submit_order` | Paper by default; live path returns `live_disabled` |
| `RiskConfig` | `LIVE_TRADING=false`, `DRY_RUN=true` defaults |
| `live_trading_enabled()` | Requires both flags |

### Polymarket (`src/adapters/polymarket_live.py`)

| Component | Status |
|---|---|
| Live send gates | `POLYLENS_POLYMARKET_LIVE_SENDS_ENABLED` env flag |
| Credential handling | Env vars only, not in code |

### Risk Engine (`src/risk/engine.py`)

| Component | Status |
|---|---|
| `RiskEngine.evaluate()` | Position limits, daily loss, halts |
| `trading_halts` table | Global/venue halt support |
| Persistent exposure tracking | Exists |

### Paper Trading (`src/analysis/paper_trading_engine.py`, `paper_copy_trader`)

| Component | Status |
|---|---|
| Autonomous paper service | `polylens-paper-trading.service` |
| Paper copy outcomes | Wallet signal validation data |

### Short Crypto (`src/analysis/short_crypto_executor.py`)

| Component | Status |
|---|---|
| Live gates | `POLYLENS_LIVE_TRADING`, `POLYLENS_CONFIRM_RISK_ACK` |
| File kill switch | `.kill_switch` path |

### Systemd Units

Most autonomous units set `POLYLENS_LIVE_TRADING=false`. `polylens-live-arb.service` is continuous scanner (separate concern). Paper/wallet units are paper-only.

### Existing Safety Gates

- `LIVE_TRADING` + `DRY_RUN` dual flag
- `POLYLENS_LIVE_TRADING`, `POLYLENS_CONFIRM_RISK_ACK`
- Exchange-specific send flags
- Risk engine halts and limits
- Kill switch file (short crypto)

## Gaps (This Branch)

1. Unified `TradingReadinessReport`
2. Explicit `execution_gate` with all conditions
3. Strategy approval registry
4. Simulated order router (dry-run audit trail)
5. Persistent kill switch with CLI
6. Dashboard visibility
7. Systemd DRY_RUN verification tests

## Architecture

```
trading_readiness.py     → TradingReadinessReport
execution_gate.py        → blocked unless ALL live conditions met
strategy_approval.py     → human-approved strategies only
simulated_order_router.py → dry-run order log
kill_switch.py           → global/strategy/wallet/market halts
```

## Risks Before Going Live

- Multiple overlapping flag namespaces (`LIVE_TRADING` vs `POLYLENS_LIVE_TRADING`)
- Kalshi live path stubbed but must remain blocked
- Strategy approval must precede any future live enablement
- Credential presence must never persist values

## Success Criteria

Live trading remains disabled; every path blocked by default; human approval required; full test coverage.
