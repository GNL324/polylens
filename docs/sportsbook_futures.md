# Sportsbook Futures

Polylens can ingest sportsbook futures/outrights from The Odds API and compare them with Polymarket championship futures.

## Commands

```bash
python -m src.cli fetch-futures --sport basketball_nba
python -m src.cli fetch-futures --sport basketball_nba --json
python -m src.cli scan-live-arb --sport basketball_nba --keyword knicks --json
python -m src.cli explain-live-matches --sport basketball_nba --keyword knicks --json
```

## Normalized Shape

Futures are normalized into rows with:

- `market_type`: `championship_winner`, `conference_winner`, `division_winner`, or `season_award`
- `league`: normalized league such as `NBA`
- `team`: outcome/team name
- `odds` and `implied_probability`
- `bookmaker_name`, `last_update`, and raw payload

## Matching Behavior

Polylens matches championship futures only when the Polymarket market and sportsbook future have:

- same league
- same team
- same market type (`championship_winner`)
- same season when both sides expose a season year

Championship futures reject game winners, spreads, totals, player props, conference winners, division winners, and awards. Existing game-line matching remains unchanged.

## Limitations

Availability depends on The Odds API and bookmaker coverage. Some books expose futures under different sport keys or omit season metadata, so Polylens only enforces season equality when both sides provide it.
