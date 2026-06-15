# Real Wallet Ingestion — Validation Report

Branch: `feature/real-wallet-ingestion`

Date: 2026-06-15

## Summary

Replaced dummy-wallet bootstrapping with real-wallet-only production ingestion and a synthetic wallet filter that excludes fixture addresses from alpha, promotion, watchlists, and acquisition.

## Before

| Issue | Status |
|---|---|
| Dummy wallets in `seed_wallets.json` | 2 synthetic + 1 real |
| Synthetic in alpha rankings | Yes |
| Synthetic in promotion path | Yes |
| Synthetic in production watchlists | Yes |
| Synthetic rejection tracking | No |

## After

| Metric | Result |
|---|---|
| Production seed file | `data/traders/real_seed_wallets.json` (real only) |
| Dummy exports in production | Removed from `data/traders/seed_exports/` |
| Dummy fixtures for tests | Retained in `tests/fixtures/wallet_forensics/` |
| `real_wallet_count` after bootstrap | > 0 |
| Synthetic in alpha rankings | Excluded |
| Synthetic in promotion | Excluded |
| Synthetic rejection tracking | Yes (`synthetic_rejected` in acquisition) |

## Architecture

```
real_seed_wallets.json + real exports
        │
        ▼
wallet_synthetic_filter.py  ──► block synthetic in production
        │
        ├── wallet_seed_import / real_wallet_ingestion
        ├── wallet_quality_filter (acquisition)
        ├── wallet_alpha_lab (rankings)
        ├── wallet_performance / feedback (promotion)
        └── wallet_tracker (watchlists)
```

## Synthetic Filter Rules

- Known fixture wallets (`0xaaaa…`, `0xbbbb…`)
- Repeated-character addresses (e.g. `0xcccc…`)
- Metadata flagged as synthetic/fixture/test

Synthetic wallets remain usable in tests and fixture files.

## Real Wallet Sources

1. `data/traders/real_seed_wallets.json`
2. `data/traders/seed_exports/market_maker_wallet.json` (real wallet)
3. Existing `data/wallets/*_activity.json` (non-synthetic)
4. Discovery/registry outputs (filtered)

## CLI Visibility

- `wallet-bootstrap-health` — real/synthetic counts, synthetic rejections
- `wallet-source-stats` — source breakdown + synthetic rejection stats
- `wallet-alpha-rankings` — `real_wallet_only: true`

## Test Results

```
1009 passed (full suite including real wallet ingestion tests)
```

New tests: `tests/test_real_wallet_ingestion.py`, updated `tests/test_wallet_seed_import.py`

## Safety

- Read-only / paper-only / analytics-only
- No live trading, execution, credentials, or private APIs

## Remaining Notes

- Network expansion via `analyze-trader` / `discover-traders` still requires manual seed or live public API for new real wallets beyond packaged seeds
- Existing production DBs may retain historical synthetic rows until cleaned; new ingestion paths reject and exclude them from rankings
