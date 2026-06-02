# Sportsbook Adapter

Polylens can fetch sportsbook odds from The Odds API and compare normalized sportsbook lines against structured Polymarket sports markets.

## Configuration

Set your API key before calling live provider endpoints:

```bash
export ODDS_API_KEY=your_key_here
```

Tests use mocked responses and do not require an API key. If `ODDS_API_KEY` is missing during live use, Polylens exits with a clear error.

## Commands

```bash
source /home/noel/.venv/bin/activate
python -m src.cli list-sportsbooks
python -m src.cli fetch-odds --sport basketball_nba
python -m src.cli fetch-odds --sport basketball_nba --bookmaker draftkings --region us --json
python -m src.cli scan-sportsbook-arb <wallet> --sport basketball_nba
```

Supported initial markets are `h2h`, `spreads`, `totals`, and `outrights` where The Odds API supports them for a sport.

## Limitations

- Sportsbook matching currently focuses on structured sports markets with recognizable league/team/opponent fields.
- Price-aware sportsbook comparisons use sportsbook implied probability and wallet-trade Polymarket implied price, not a live Polymarket order book.
- Edges are indicative only and exclude fees, limits, slippage, void rules, timing differences, and account constraints.
- `scan-arb` is unchanged; sportsbook scans use `scan-sportsbook-arb` independently.
