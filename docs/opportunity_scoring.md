# Opportunity Execution Scoring

Polylens ranks live arbitrage candidates by an `execution_score` from 0 to 1. The score is intended to prioritize opportunities that are more usable, not merely the largest theoretical edge.

## Score inputs

- `estimated_edge`: positive indicative edge receives the largest weight.
- Liquidity/depth: venue liquidity is rewarded when available; missing or zero liquidity is penalized.
- Match confidence: structured high-confidence matches score above low-confidence text matches.
- Freshness: recent price updates score higher than stale timestamps.
- Time to close: markets that are already closed or closing very soon are penalized.
- Missing data: incomplete price, liquidity, or update-time fields lower the score.

## Candidate fields

Scored candidates preserve existing fields and add: `raw_edge`, `liquidity_score`, `confidence_score`, `freshness_score`, `time_score`, `execution_score`, and `score_reason`.

## Filtering

`scan-live-arb` supports:

```bash
python -m src.cli scan-live-arb --keyword bitcoin --min-edge 0.02 --min-score 0.55
python -m src.cli scan-live-arb --sport basketball_nba --max-close-hours 48 --include-low-confidence
```

The JSON output includes candidate counts before and after filtering plus filter reasons. The scanner remains conservative: low-confidence candidates are excluded by default unless `--include-low-confidence` is set.

## Limitations

Execution scoring is an indicative ranking layer. It does not prove fillability and does not include fees, slippage, account limits, settlement risk, withdrawal delays, or hedging availability.
