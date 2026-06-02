# Multibook And Synthetic Field Arbitrage

Polylens can scan sportsbook-vs-sportsbook arbitrage and synthetic futures field structures.

## Two-Way Sportsbook Arbitrage

For a two-outcome event, Polylens selects the best implied probability for each side across books. If the sum is below 1.00, the opportunity is classified as `true_arbitrage` and includes guaranteed ROI and stake sizing.

## Synthetic Field Arbitrage

For championship futures, Polylens can build a synthetic field from all teams other than the selected team. It only classifies true arbitrage when all outcomes are covered. Partial fields remain incomplete hedges.

## CLI

```bash
python -m src.cli scan-multibook-arb --sport basketball_nba --bankroll 1000 --json
```

Diagnostics include `no opposite side found`, `incomplete field`, `missing team outcomes`, `total implied probability >= 1`, `settlement mismatch`, `stale odds`, and `bookmaker mismatch` when detectable.


## Synthetic Field Debugging

Use the debug command to inspect deduped futures fields:

```bash
python -m src.cli debug-synthetic-field --sport basketball_nba --team Knicks --json
```

The field builder keeps only the best implied probability per unique outcome across all books. Diagnostics include `unique_outcomes`, `duplicate_outcomes_removed`, `books_considered`, `best_price_source`, field members, and the final implied probability sum.
