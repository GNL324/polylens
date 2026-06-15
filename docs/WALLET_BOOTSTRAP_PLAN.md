# Wallet Bootstrap Plan

Branch: `feature/wallet-seed-and-ingestion`

## Problem

The wallet intelligence pipeline is operational but empty:

| Subsystem | Cold-start count |
|---|---|
| Wallet Acquisition | 0 |
| Wallet Discovery | 0 |
| Wallet Registry | 0 |
| Wallet Signals | 0 |
| Wallet Alpha | 0 |

## Root Cause

All acquisition and discovery sources read from data that does not exist on first run:

1. **`data/traders/watchlist.json` is missing** — `ManualWalletSource`, `WatchlistAcquisitionSource`, and `WatchlistDiscoverySource` silently return `[]`.
2. **`data/traders.db:wallets` is empty** — registry-backed sources have nothing to read.
3. **`data/trader_discovery.db:discovered_wallets` is empty** — no prior discovery run with seed input.
4. **`seed_wallet` discovery source is disabled by default** — autonomy never passes a seed wallet.
5. **Circular dependency** — acquisition reads discovery/registry → discovery reads watchlist/registry → watchlist missing and registry empty → perpetual zero results.

## Existing Tools (Not Wired for Cold Start)

| Tool | Role | Gap |
|---|---|---|
| `wallet_forensics` | Offline classifier from activity JSON | Requires pre-exported JSON |
| `analyze_trader` | Export + forensics + registry | Requires manual wallet + live API |
| `scan_top_traders` | Batch scan from watchlist/registry | Watchlist missing |
| `trader_registry` | Persistent wallet store | Never seeded automatically |
| `wallet_data_acquisition` | Multi-source ingestion | All sources empty |
| `wallet_discovery` | Candidate expansion | No seed input |

## Solution Architecture

```
seed_wallets.json + seed_exports/*.json
        │
        ▼
wallet_seed_import.py  ──► registry + discovery + watchlist
        │
        ▼
wallet_bootstrap.py    ──► top-trader ingestion + acquisition run
        │
        ▼
wallet_autonomy_service bootstrap cycle (once when empty)
```

## Phases

1. **Seed import framework** — import known wallets, exports, forensic reports, registry snapshots with deduplication and source attribution.
2. **Forensics integration** — convert local activity JSON into registry entries, discovery candidates, and watchlist rows without API calls.
3. **Top-trader ingestion** — promote registry wallets into discovery and acquisition candidates.
4. **Bootstrap health** — metrics for seed imports, registry/discovery population, ingestion success rate.
5. **Autonomy integration** — bootstrap cycle runs once when ecosystem is empty; idempotent on restart.

## Safety

- Read-only analytics only
- No live trading, execution, credentials, or exchange order placement
- Offline-first bootstrap from bundled seed exports

## Success Criteria

After bootstrap on empty system:

- `registry wallets > 0`
- `registry_growth > 0`
- `wallet_acquisition_records > 0`
- `discovery candidates > 0`
