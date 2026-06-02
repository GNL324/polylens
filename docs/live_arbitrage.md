# Live Arbitrage Discovery

`scan-live-arb` scans currently discoverable venue inventory without requiring a wallet address. It compares live Polymarket Gamma markets, public Kalshi markets, and sportsbook odds when The Odds API is configured.

## Command

```bash
python -m src.cli scan-live-arb --keyword bitcoin --limit 50
python -m src.cli scan-live-arb --sport basketball_nba --region us --bookmaker draftkings --json
```

## Options

- `--keyword`: Polymarket Gamma search text.
- `--category`: Polymarket category filter when supported by Gamma.
- `--sport`: sport key for sportsbook odds and Polymarket tag lookup, such as `basketball_nba`.
- `--limit`: maximum live markets requested per page/source.
- `--region`: The Odds API region, default `us`.
- `--bookmaker`: optional sportsbook filter.
- `--json`: emit machine-readable output.

## Output

The scan reports markets scanned by venue, matches found by venue pair, candidate count, top candidates sorted by estimated edge, and skipped/rejected reason counts. Missing sportsbook credentials only skip the sportsbook side; Polymarket/Kalshi scanning continues.

## Limitations

Live venue APIs expose indicative data that may be stale or incomplete. The scanner is conservative and will return no candidate rather than inventing a match when structured sports/crypto fields, pricing, or timing are ambiguous. Reported edges do not include fees, slippage, withdrawal costs, or executable depth.
