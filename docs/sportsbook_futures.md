# Sportsbook Futures

Polylens can ingest sportsbook futures/outrights from The Odds API and compare them with Polymarket championship futures. The Odds API only supports outrights for sport keys whose `/sports` entry has `has_outrights: true`; regular game-line keys such as `basketball_nba` may not support `markets=outrights` directly. Polylens discovers an outright-capable sport key first and returns an unsupported response when none is available.

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

## Unsupported Futures

When The Odds API has no outright-capable sport key for the requested sport, `fetch-futures` returns:

```json
{
  "supported": false,
  "reason": "no futures endpoint for sport",
  "futures": []
}
```

`scan-live-arb` only includes futures matching when normalized futures inventory is present. Game-line `h2h`, `spreads`, and `totals` are excluded from futures diagnostics.


## League Normalization

Futures sport keys are normalized by prefix. For example, `basketball_nba_championship_winner` maps to league `NBA`, `americanfootball_nfl_super_bowl_winner` maps to `NFL`, `baseball_mlb_world_series_winner` maps to `MLB`, and `icehockey_nhl_championship_winner` maps to `NHL`. Championship labels such as NBA Championship Winner, NBA Finals Winner, and 2026 NBA Champion normalize to `championship_winner`.
