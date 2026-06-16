# Real Wallet Ingestion Plan

Branch: `feature/real-wallet-ingestion`

## Current Dummy Wallet Issue

Production bootstrap previously imported three seed wallets, including two synthetic fixtures:

| Wallet | Type |
|---|---|
| `0x927f7694de44d19a72bce76254e628d1c141d215` | Real (from forensic export) |
| `0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa` | Synthetic fixture |
| `0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb` | Synthetic fixture |

Synthetic wallets entered registry, discovery, watchlists, alpha rankings, and promotion paths because no production filter existed.

## Where Synthetic Wallets Entered Production

| Path | Issue |
|---|---|
| `data/traders/seed_wallets.json` | Listed dummy addresses |
| `data/traders/seed_exports/` | Contained fixture JSON for dummy wallets |
| `wallet_seed_import.py` | Imported all seeds without filtering |
| `wallet_data_acquisition.py` | Accepted synthetic wallets into registry |
| `wallet_alpha_lab.py` | Ranked synthetic wallets |
| `wallet_tracker.py` | Included synthetic wallets in watchlists |
| `wallet_feedback_engine.py` | Could promote synthetic wallets |

## Real Wallet Source Options

| Source | Read-only | Credentials | Status |
|---|---|---|---|
| Packaged real forensic export | Yes | No | Implemented |
| `data/traders/real_seed_wallets.json` | Yes | No | Implemented |
| Existing `data/wallets/*_activity.json` | Yes | No | Implemented |
| Discovery DB outputs | Yes | No | Filtered |
| Registry snapshots | Yes | No | Filtered |
| `analyze-trader` / Polymarket public activity | Yes | No | Existing CLI (manual) |
| `scan-top-traders` / watchlist | Yes | No | Existing CLI |

## Safest Ingestion Path

1. **`wallet_synthetic_filter.py`** — block synthetic wallets from production paths
2. **`real_seed_wallets.json`** — real-wallet-only production seeds
3. **`real_wallet_ingestion.py`** — ingest real exports and seeds
4. **Acquisition quality filter** — reject synthetic with tracked reason
5. **Alpha / performance / feedback / watchlist** — exclude synthetic wallets

## Safety

- Read-only analytics only
- No live trading, execution, credentials, or private APIs
- Synthetic wallets remain in `tests/fixtures/wallet_forensics/` for tests

## Success Criteria

- Dummy wallets excluded from alpha, promotion, watchlists
- Real wallet count > 0 after bootstrap
- Synthetic rejections tracked separately in acquisition reports
