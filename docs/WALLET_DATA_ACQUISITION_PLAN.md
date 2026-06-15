# Wallet Data Acquisition — Integration Plan

Branch: `feature/wallet-data-acquisition`

## Objective

Build a Wallet Data Acquisition Layer that continuously discovers, ingests, normalizes, validates, and enriches real trader wallets for the Wallet Intelligence System — paper-only, read-only, orchestrating existing tools.

## Existing Acquisition Paths Audit

### wallet_forensics (`src/analysis/wallet_forensics.py`)

| Capability | Status |
|---|---|
| Activity parsing and classification | Exists |
| `build_wallet_forensics_report()` | Exists |
| Metrics: trade_count, overlap, arbitrage | Exists |

**Role:** Enrichment source via existing registry reports (forensics output already saved by `analyze_trader`).

### analyze_trader (`src/cli.py` → `analyze_trader_cli`)

| Capability | Status |
|---|---|
| Export wallet activity (read-only API) | Exists |
| Build forensics report | Exists |
| `save_wallet_report()` to registry | Exists |

**Role:** Reference ingestion path; acquisition reuses registry outputs, does not duplicate activity export in hot path.

### scan_top_traders (`scan_trader_wallets` / `scan_wallets`)

| Capability | Status |
|---|---|
| Scan watchlist + registry wallets | Exists |
| Activity export to `data/wallets/` | Exists |

**Role:** Candidate seeding via watchlist source connector.

### trader_discovery (`src/analysis/trader_discovery.py`)

| Capability | Status |
|---|---|
| `TraderDiscoveryCandidate` | Exists |
| `discover_from_registry`, activity export discovery | Exists |
| `discovered_wallets` table | Exists in `trader_discovery.db` |

### Registry (`src/analysis/trader_registry.py`)

| Capability | Status |
|---|---|
| `wallets`, `wallet_reports` tables | Exists |
| `save_wallet_report()`, `list_traders()` | Exists |
| first_seen, last_seen, confidence, watch_score | Exists |

### WalletDiscovery / WalletTracker / Autonomy

| Component | Current role |
|---|---|
| `WalletDiscoveryEngine` | Multi-source discovery, scoring, lifecycle |
| `WalletTracker` | Watchlist ranking and persistence |
| `WalletAutonomyService` | Scheduled discovery/signals/performance cycles |

**Gap:** No dedicated acquisition pipeline with quality filtering, source attribution tracking, and acquisition analytics.

## Component Classification

| Proposed | Approach |
|---|---|
| Acquisition engine | **New** — orchestrates existing sources |
| Source connectors | **New abstraction** — wraps discovery, forensics/registry, analytics |
| Quality filter | **New** — `WalletQualityScore` |
| Registry enrichment | **Extend** — `save_wallet_report`, `save_discovery_candidate`, tracker watchlist |
| Acquisition analytics | **New** — reads acquisition tables |
| Autonomy cycle | **Extend** `WalletAutonomyService` |
| Dashboard | **Extend** trader dashboard |

## Architecture

```
Source Connectors (discovery_db, registry, forensics, watchlist)
        │
        ▼
WalletDataAcquisitionEngine
        ├── validate (validate_wallet)
        ├── normalize
        ├── WalletQualityFilter
        └── enrich → trader_registry + discovery_db + watchlist
        │
        ▼
wallet_acquisition_records (traders.db)
        │
        ▼
WalletAutonomyService acquisition cycle (6h)
```

## Out of Scope

- Live trading, order placement, execution paths, credentials
- New signal engine or portfolio engine
- Kalshi/Polymarket execution changes

## Success Criteria

Registry grows automatically, discovery receives real candidates, autonomy acquires data, dashboard visibility, full tests.
