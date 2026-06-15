# Polylens Data Path Incident Report

**Incident date:** 2026-06-15 UTC  
**Host:** Predix (`192.168.68.62`)  
**Repository:** https://git.noelgrca.com/Noel-Lab/polylens.git  
**Branch:** `main` (commit `7fa78a6d8ccf4afa54a30f4e53db759ef5d2d7f9` after merge)

## Issue

SRE monitoring reported `/home/noel/polylens/traders.db` exists but is **0 bytes and contains no tables**, while the Wallet Autonomy Service reported healthy with successful cycles.

## Root Cause

- **Polylens canonical DB path is `/home/noel/polylens/data/traders.db`** (defined in `src/analysis/trader_registry.py` as `DEFAULT_TRADERS_DB = "data/traders.db"`).
- **SRE monitoring was checking the wrong file:** it queried `/home/noel/polylens/traders.db` (empty root-level file) instead of `data/traders.db`.
- The empty root-level `/home/noel/polylens/traders.db` was an accidental file, likely created by a command or process run from `/home/noel/polylens` that used a relative path and defaulted to the current directory rather than `data/`.
- Wallet Autonomy Service, Trader Dashboard, and all code paths were always writing to and reading from `/home/noel/polylens/data/traders.db`, which is populated and healthy.

## DB Paths Found

```text
/home/noel/polylens/traders.db                          0 bytes, empty (accidental)
/home/noel/polylens/data/traders.db                319,488 bytes, 17 tables (canonical)
/home/noel/polylens/data/trader_discovery.db        53,248 bytes
/home/noel/polylens/data/trader_signals.db       155,648 bytes
/home/noel/polylens/data/paper_copy_trader.db     73,728 bytes
/home/noel/polylens/data/polylens.db             356,352 bytes
/home/noel/polylens/data/opportunities.db          53,248 bytes
/home/noel/polylens/data/short_crypto_paper.db 5,238,784 bytes
```

## Canonical DB Path Evidence

- `src/analysis/trader_registry.py`: `DEFAULT_TRADERS_DB = "data/traders.db"`
- `src/analysis/trader_discovery.py`: `DEFAULT_TRADER_DISCOVERY_DB = "data/trader_discovery.db"`
- `WalletDataAcquisitionEngine.traders_db_path` resolves to `data/traders.db`
- `WalletAutonomyService.traders_db_path` resolves to `data/traders.db`
- `wallet-service-run` and `wallet-service-health` both read from `data/traders.db`

## Tables in Canonical DB

```text
sqlite_sequence
strategy_profiles
wallet_acquisition_records
wallet_acquisition_runs
wallet_autonomy_reports
wallet_lifecycle
wallet_lifecycle_events
wallet_performance_actions
wallet_performance_snapshots
wallet_performance_trends
wallet_reports
wallet_score_history
wallet_service_cycle_runs
wallet_service_state
wallet_watchlist
wallets
```

## Row Counts Before/After Fix

### Before (root-level empty file)
- `/home/noel/polylens/traders.db`: 0 tables, 0 rows

### Canonical `data/traders.db` before re-run
```text
wallet_service_state:         7 rows
wallet_service_cycle_runs:   39 rows
wallet_autonomy_reports:      3 rows
wallet_acquisition_runs:      2 rows
wallet_acquisition_records:   0 rows
wallet_lifecycle:             0 rows
wallet_score_history:         0 rows
```

### Canonical `data/traders.db` after re-run
```text
wallet_service_state:         7 rows
wallet_service_cycle_runs:   45 rows
wallet_autonomy_reports:      4 rows
wallet_acquisition_runs:      3 rows
wallet_acquisition_records:   0 rows
```

Row counts increased, confirming the service is writing to `data/traders.db`.

## Fix Applied

1. **Backed up the accidental empty file:**
   ```bash
   mv /home/noel/polylens/traders.db /home/noel/polylens/traders.db.empty.20260615.bak
   ```

2. **Confirmed no populated data was lost:** The canonical `data/traders.db` remained intact and all row counts increased after running `wallet-service-run`.

3. **SRE monitoring updated:** Both the 30-minute SRE review and the daily operations report jobs were already using `wallet-service-health` and CLI commands that read from `data/traders.db`. The anomaly was caused by an ad-hoc SRE check that directly inspected `/home/noel/polylens/traders.db` rather than the canonical `data/traders.db`. The cron job prompts now explicitly instruct using the canonical path `data/traders.db` for any direct SQLite queries.

## Validation Evidence

### 1. Default path resolution
```text
trader_registry.DEFAULT_TRADERS_DB = data/traders.db
trader_discovery.DEFAULT_TRADER_DISCOVERY_DB = data/trader_discovery.db
WalletDataAcquisitionEngine.traders_db_path = data/traders.db
WalletAutonomyService.traders_db_path = data/traders.db
WalletAutonomyService.discovery_db_path = data/trader_discovery.db
```

### 2. `wallet-service-run --force` output
- `cycles_run: 6`
- `read_only: true`
- `paper_only: true`
- All 6 cycles completed with status `success`

### 3. `wallet-service-health --json` output
- `status: healthy`
- All cycles: `health_status: healthy`, `stale: false`, `last_status: success`
- `avg_latency_ms: 313.34`

### 4. Row count increase
- `wallet_service_cycle_runs`: 39 → 45 (+6)
- `wallet_autonomy_reports`: 3 → 4 (+1)
- `wallet_acquisition_runs`: 2 → 3 (+1)

### 5. Service status
- `wallet-autonomy.timer`: active (waiting), next trigger scheduled
- `polylens-trader-dashboard.service`: active (running) on `127.0.0.1:8788`

### 6. Empty file removed
```text
ls /home/noel/polylens/traders.db
ls: cannot access '/home/noel/polylens/traders.db': No such file or directory
```

## Safety and Security

- Live trading remains disabled: `POLYLENS_LIVE_TRADING=false` in `wallet-autonomy.service`
- Autonomous crypto remains disabled: `POLYLENS_AUTONOMOUS_CRYPTO=false`
- No credentials were exposed or modified
- No execution paths were changed
- Only an empty accidental file was moved to a backup name
- No populated DB was deleted

## Remaining Risks

1. **Path confusion could recur** if any future ad-hoc command or cron job is run from `/home/noel/polylens` and assumes `traders.db` is in the current directory. Mitigation: the SRE cron job prompt now explicitly references `data/traders.db`.

2. **Dashboard code and systemd units** all use canonical paths, so no code changes were required.

3. **No data loss** occurred; the canonical DB was never at risk.

## Recommendation

- Add an operational note to `POLYLENS_OPERATIONS_REPORT.md` and SRE playbooks: all Polylens persistent SQLite state lives under `data/`, not the repo root.
- Consider adding a startup check in `WalletAutonomyService` that warns if a root-level `traders.db` exists alongside `data/traders.db`, to surface this class of path mismatch earlier.

## Final Status

✅ Root cause identified: SRE was reading the wrong path.  
✅ Accidental empty DB backed up and removed.  
✅ Canonical `data/traders.db` confirmed populated and receiving writes.  
✅ Wallet Autonomy Service healthy and all cycles succeeding.  
✅ No live trading, no execution path changes, no data loss.  
✅ Trader Dashboard continues to read the canonical DB.  

**Incident resolved.**
