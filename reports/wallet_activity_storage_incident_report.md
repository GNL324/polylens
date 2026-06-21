# Wallet Activity Storage Incident Report

## Root Cause

`save_wallet_activity_export` appended one full `wallet_exports.export_json` blob for every wallet refresh. The autonomy refresh path repeatedly exported the same wallets, while `wallet_events` used `INSERT OR IGNORE` on `event_key` and therefore deduplicated the normalized event intelligence. The result was unbounded raw snapshot growth without corresponding intelligence growth.

## Live Database Findings

- Original `data/wallet_activity.db`: 29,944,877,056 bytes, shown as 28G by `du`.
- `wallet_exports`: 40,289 rows before final compaction attempt.
- `wallet_events`: 1,055,867 rows.
- `wallet_exports` raw JSON: about 27.1GB.
- `wallet_events.raw_json`: about 888MB.
- Latest wallet/source export groups: 74.
- Top wallets had about 800 repeated exports each, often hundreds of MB of repeated JSON per wallet.

## Code Remediation

`src/analysis/wallet_activity.py` now treats `wallet_exports` as a latest snapshot table by wallet/source. Repeated exports update the latest row instead of appending another full JSON blob. Historical intelligence remains in deduplicated `wallet_events`.

Regression coverage was added in `tests/test_wallet_activity.py` to verify repeated exports keep a single `wallet_exports` row while preserving unique normalized events.

## Emergency Cleanup

Added `scripts/compact_wallet_activity_db.py`.

The script:

- inspects the source database;
- optionally copies a full backup when a backup directory has enough space;
- builds a compact replacement database in a work directory such as `/dev/shm`;
- keeps only the latest `wallet_exports` row per wallet/source;
- preserves all `wallet_events`;
- validates counts before replacement.

Because the root filesystem had less than 500MB free, a full 28GB backup could not be created locally. The compact database was built and verified in `/dev/shm`, then installed as `data/wallet_activity.db`.

## Recovery

- Compacted `data/wallet_activity.db`: 2.7GB.
- Root filesystem after cleanup: 26GB free.
- Estimated recovered disk: about 25GB.
- Final integrity check: `ok`.
- Final row counts:
  - `wallet_exports`: 74
  - `wallet_events`: 1,055,867

## Rollback

If a full backup directory is available in future incidents:

```bash
.venv/bin/python scripts/compact_wallet_activity_db.py \
  --db-path data/wallet_activity.db \
  --backup-dir /path/with/30g/free \
  --apply
```

Rollback from a full backup:

```bash
cp /path/with/30g/free/wallet_activity.backup.TIMESTAMP.db data/wallet_activity.db
```

For this emergency run, no full local backup was possible. The authoritative rollback point is the compacted database plus the preserved normalized `wallet_events` intelligence.

## Retention Recommendation

Keep `wallet_events` as the durable historical intelligence table. Keep only the latest export snapshot per wallet/source in `wallet_exports`. Do not retain historical raw export blobs in SQLite unless they are compressed and stored outside the hot activity database with a storage-pressure lifecycle policy.

