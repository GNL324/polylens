# Live Match Diagnostics

`explain-live-matches` explains why live Polymarket, Kalshi, and sportsbook markets did or did not match. It is intended for debugging cases where sportsbook odds are ingested but `scan-live-arb` reports zero sportsbook matches.

## Command

```bash
python -m src.cli explain-live-matches --sport basketball_nba --keyword knicks --limit 50
python -m src.cli explain-live-matches --sport basketball_nba --json --rejected-only
```

## Output

The command reports attempted, accepted, and rejected match counts, top rejection reasons, and sample rejected comparisons with parsed fields. Sportsbook rejections use specific reasons such as `team mismatch`, `opponent mismatch`, `league mismatch`, `market type mismatch`, `spread mismatch`, `total mismatch`, `event date mismatch`, `confidence below threshold`, and `missing structured fields`.

`scan-live-arb --json` also includes a compact `live_match_summary` with attempted, accepted, rejected, and rejection-count fields.
