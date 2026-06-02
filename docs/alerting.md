# Live Arbitrage Alerting

`watch-live-arb` repeatedly runs the live arbitrage scanner and sends alerts for qualifying opportunities. It uses the same execution scoring and filters as `scan-live-arb`.

## Console alerts

By default, alerts print to the console:

```bash
python -m src.cli watch-live-arb --once --keyword bitcoin --min-edge 0.02 --min-score 0.6
python -m src.cli watch-live-arb --interval 60 --sport basketball_nba --min-score 0.7
```

## Webhook alerts

Set a webhook URL and pass `--webhook`:

```bash
export POLYLENS_WEBHOOK_URL="https://example.com/webhook"
python -m src.cli watch-live-arb --once --webhook --sport basketball_nba --min-edge 0.02
```

If `--webhook` is passed without `POLYLENS_WEBHOOK_URL`, Polylens exits with a clear error. Discord-compatible webhook URLs receive a Discord-shaped payload; other URLs receive the alert payload as JSON.

## Flags

- `--interval`: seconds between scans in continuous mode.
- `--once`: run one scan and exit.
- `--min-edge`: minimum estimated edge.
- `--min-score`: minimum execution score.
- `--max-close-hours`: only alert on markets closing within this many hours.
- `--sport`, `--keyword`, `--category`, `--bookmaker`, `--region`: passed through to live scanning.
- `--json`: emit watch results as JSON.

## Duplicate suppression

Watch mode suppresses repeat alerts inside a timestamp bucket using venue pair, market identifiers, edge, execution score, and bucket time. This prevents the same opportunity from being sent repeatedly while preserving alerts when the edge or score changes.

## Alert payload

Alerts include title, venue pair, market names, edge, execution score, score reason, prices/odds, close time, timestamp, and the full candidate object.
