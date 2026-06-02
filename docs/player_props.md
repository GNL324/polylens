# Player Prop Arbitrage

Polylens supports common NBA player prop markets from The Odds API:

- player_points
- player_rebounds
- player_assists
- player_threes
- player_blocks
- player_steals
- player_turnovers
- player_points_rebounds_assists
- player_points_rebounds
- player_points_assists
- player_rebounds_assists

## Commands

```bash
python -m src.cli fetch-player-props --sport basketball_nba --json
python -m src.cli fetch-player-props --sport basketball_nba --event-id <event_id> --markets player_points,player_assists
python -m src.cli scan-prop-arb --sport basketball_nba --bankroll 1000 --json
```

## Matching

Props are equivalent when sport/league, event, player, prop type, and line match. Over and under sides are paired as hedge legs. A true prop arbitrage exists when over implied probability plus under implied probability is below 1.00.

Diagnostics include player mismatch, line mismatch, market mismatch, same side, total implied probability >= 1, and stale odds when available.


## Odds API Workflow

The Odds API exposes player props through event-level odds, not the sport-level odds endpoint.

1. Discover events:

```bash
GET /v4/sports/{sport}/events
```

2. Fetch props for each event:

```bash
GET /v4/sports/{sport}/events/{eventId}/odds?markets=player_points,player_rebounds
```

`fetch-player-props` performs this workflow automatically and aggregates normalized props across events. Unsupported prop markets are reported in diagnostics instead of raising.

## Debugging

```bash
python -m src.cli debug-player-props --sport basketball_nba --json
```

The debug output includes event id, teams, available prop markets, prop count, discovered/scanned/failed event counts, and rejected markets.
