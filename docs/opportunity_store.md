# Persistent Opportunity Storage

Polylens can persist live scan results, opportunities, alerts, and rejected match diagnostics to SQLite. Storage is opt-in for existing commands.

## Database

Default path:

```bash
data/polylens.db
```

Override with `--db-path` when saving or querying.

## Saving scans

```bash
python -m src.cli scan-live-arb --keyword bitcoin --min-score 0.6 --save
python -m src.cli watch-live-arb --once --keyword bitcoin --min-score 0.7 --save
python -m src.cli explain-matches 0x6de0cdb03b9e49dcc58c879249a280cbd52b436c --save
```

Saved tables:

- `scan_runs`: timestamp, scan mode, filters, venues scanned, candidate count, alert count, skipped/error summaries, raw result JSON.
- `opportunities`: venue pair, market IDs/titles, raw edge, execution score, score reason, prices/odds, close time, full candidate JSON.
- `alerts`: opportunity ID, destination, sent timestamp, status, error, payload JSON.
- `rejected_candidates`: venue pair or match type, market titles, rejection reason, parsed fields, full diagnostic JSON.

## Query commands

```bash
python -m src.cli recent-opportunities --limit 10
python -m src.cli recent-alerts --limit 10
python -m src.cli opportunity-stats
```

Use `--json` for machine-readable output and `--db-path path/to.db` for a custom database.

## Notes

Commands continue to work without persistence unless `--save` is passed. Watch mode stores scan runs and alert records when enabled. `explain-matches --save` stores rejected diagnostics for later parser and matcher review.
