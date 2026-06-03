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


## Memory Safety

Normalized player prop rows intentionally do not retain full Odds API event, bookmaker, or market payloads. They keep only lightweight debug fields: `raw_outcome_name`, `raw_outcome_description`, and `raw_price`. Prop arbitrage diagnostics are compact summaries and do not embed full prop objects.


## Grouped Matching

Prop matching groups rows by sport, league, event, normalized player, market type, and line before comparing sides. Only over/under rows inside the same group are compared, so player, event, and line mismatches are eliminated before pair construction. Diagnostics retain meaningful rejects such as missing opposite side, invalid side, and total implied probability >= 1.


## Continuous Prop Scanner

```bash
python -m src.cli watch-prop-arb --sport basketball_nba --markets player_points --interval 30 --bankroll 1000 --min-roi 0.01
```

The scanner filters opportunities before output, persists new qualifying opportunities to `data/opportunities.db`, and sends Telegram alerts when `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` are configured. Duplicate alerts are suppressed for 15 minutes.
