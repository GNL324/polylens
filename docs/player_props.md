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
