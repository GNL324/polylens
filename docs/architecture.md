# Architecture

Polylens is organized as a CLI-first analytics system with separate adapters, analysis modules, reports, storage, and notifications.

## Adapters

- `src/adapters/polymarket.py`: Polymarket Gamma/Data API access, wallet trades, profiles, positions, and live market discovery.
- `src/adapters/kalshi.py`: Kalshi market ingestion and price field preservation.
- `src/adapters/odds_api.py`: The Odds API sports, events, odds, futures, and player prop ingestion.

## Analysis Modules

Wallet analytics:

- `volume.py`, `markets.py`, `timing.py`, `pnl.py`, `arb_signals.py`

Cross-market matching:

- `sports_parser.py`, `structured_matching.py`
- `crypto_parser.py`, `crypto_matching.py`
- `cross_market.py`, `match_validation.py`
- `live_match_diagnostics.py`, `market_inventory.py`

Arbitrage engines:

- `arb_pricing.py`
- `live_arbitrage.py`
- `opportunity_scoring.py`
- `hedged_arbitrage.py`, `hedge_leg_discovery.py`
- `multibook_arbitrage.py`, `synthetic_field.py`
- `prop_normalization.py`, `prop_matching.py`, `prop_arbitrage.py`

## Storage Layer

- `src/storage/opportunity_store.py`: scan-level storage for live arbitrage workflows.
- `src/storage/opportunities.py`: prop arbitrage opportunity and alert history.

## Notification Layer

- `src/alerts/notifier.py`: console/webhook alerting for live arbitrage watch mode.
- `src/notifications/telegram.py`: Telegram alerts for prop arbitrage opportunities.

## CLI Layer

`src/cli.py` exposes wallet analysis, market diagnostics, live arbitrage scans, prop scans, watch modes, and storage history commands.
