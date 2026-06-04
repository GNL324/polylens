# Kalshi Automation

Polylens includes a paper-first Kalshi automation layer for scanning simple signals and recording simulated trades.

## Safety Rules

- Paper trading is the default.
- Real orders are not placed by this release.
- `LIVE_TRADING` defaults to `false`.
- `DRY_RUN` defaults to `true`.
- Even when `LIVE_TRADING=true` and `DRY_RUN=false`, the executor returns `live_disabled` because live order placement is intentionally not implemented.
- API keys must never be hardcoded. Load secrets from `.env` or the environment.
- Order prices may be entered as dollars (`0.45`) or Kalshi-style cents (`45`). Polylens normalizes both to `0.45` internally.

## `.env` Example

```text
LIVE_TRADING=false
DRY_RUN=true
KALSHI_MAX_TRADE_DOLLARS=25
KALSHI_MAX_OPEN_EXPOSURE=100
KALSHI_MAX_DAILY_LOSS=50
KALSHI_DUPLICATE_SIGNAL_COOLDOWN_SECONDS=300
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
```

## Commands

```bash
python -m src.cli kalshi-markets --limit 10
python -m src.cli kalshi-orderbook --ticker <ticker>
python -m src.cli kalshi-paper-scan --limit 20 --max-price 0.5
python -m src.cli kalshi-paper-trade --ticker <ticker> --side yes --price 0.45 --count 1
python -m src.cli kalshi-paper-trade --ticker <ticker> --side yes --price 45 --count 1
python -m src.cli kalshi-status --json
```

## Risk Controls

- Maximum trade dollars
- Maximum open exposure
- Maximum daily loss
- Duplicate signal cooldown
- Live trading gate
- Input validation for side, price, count, and ticker

## Paper Journal

Accepted paper trades are appended to:

```text
data/raw/kalshi_paper_orders.jsonl
```

Each row is a lightweight JSON object with the simulated order, timestamp, and normalized price. This is a paper journal only; it is not an execution record from Kalshi.

## Telegram Alerts

If `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` are configured, paper orders emit a lightweight Telegram notification. Missing Telegram config is skipped without failing the trade.

## Tests

```bash
pytest
```

## Authenticated Read-Only Setup

Kalshi authenticated reads require an API key ID and a local private key file. Do not paste private key contents into `.env`.

```text
KALSHI_API_KEY_ID=00000000-0000-0000-0000-000000000000
KALSHI_PRIVATE_KEY_PATH=/home/noel/polylens/secrets/kalshi.key
KALSHI_ENV=demo
# Optional advanced override:
KALSHI_BASE_URL=https://external-api.demo.kalshi.co/trade-api/v2
```

Supported environments:

- `KALSHI_ENV=demo` uses `https://external-api.demo.kalshi.co/trade-api/v2`.
- `KALSHI_ENV=production` uses `https://external-api.kalshi.com/trade-api/v2`.
- `KALSHI_BASE_URL` can override either for testing.

The signer follows Kalshi's documented authenticated request flow: sign `timestamp + HTTP_METHOD + path` with RSA-PSS/SHA256, excluding query parameters from the signed path. The key file path may be logged in setup errors, but key contents and signatures are never logged.

Install `cryptography` in the virtual environment before using authenticated reads if it is not already present:

```bash
source /home/noel/.venv/bin/activate
pip install cryptography
```

## Authenticated Read-Only Commands

```bash
python -m src.cli kalshi-account --json
python -m src.cli kalshi-balance --json
python -m src.cli kalshi-positions --limit 100 --json
python -m src.cli kalshi-orders --limit 100 --json
```

These commands only use GET endpoints. Live order placement and order cancellation remain disabled. Any attempted write helper returns `write_blocked` and does not call Kalshi.

## Local Secret Files

Keep Kalshi private keys in a local, untracked directory:

```bash
mkdir -p /home/noel/polylens/secrets
chmod 700 /home/noel/polylens/secrets
```

The `secrets/` directory and `.env` file are ignored by git. Expected `.env` values are:

```text
KALSHI_API_KEY_ID=
KALSHI_PRIVATE_KEY_PATH=/home/noel/polylens/secrets/kalshi.key
KALSHI_ENV=demo
# or: KALSHI_ENV=production
```

## Account Analytics

```bash
python -m src.cli kalshi-report --json
python -m src.cli kalshi-export --json
python -m src.cli kalshi-patterns --json
```

`kalshi-report` fetches balance, positions, orders, and fills, then summarizes account balance, open positions, realized PnL, fees paid, trades by market type, trades by crypto asset, win rate, average entry price, and average contract size.

`kalshi-export` writes:

```text
data/reports/kalshi_report.json
data/reports/kalshi_report.csv
```

`kalshi-patterns` highlights repeated trading behavior, frequently traded markets, high ROI trades, worst trades, scalp-like behavior, and possible arbitrage behavior. These are read-only analytics and should be treated as heuristics.

## Strategy Simulation

```bash
python -m src.cli kalshi-simulate   --assets BTC,ETH,SOL   --market-types crypto   --price-bands 0.01-0.10,0.90-0.99   --max-contracts 5   --bankroll 1000   --fee-assumption 0.01   --strategy-mode extreme-probability   --export
```

Supported modes:

- `extreme-probability`
- `mean-reversion`
- `momentum`
- `no-trade-baseline`

The simulator uses authenticated historical Kalshi fills when available, then filled orders as a fallback. It reports simulated PnL, fees, win/loss count, max drawdown, average trade size, best/worst trade, and a strategy classification. It can export:

```text
data/reports/kalshi_simulation.json
data/reports/kalshi_simulation.csv
```

This is a read-only backtest-style tool. It does not place or cancel orders.

## Market Data Recorder

Collect read-only Kalshi market and orderbook snapshots before considering live trading:

```bash
python -m src.cli kalshi-record-markets   --assets BTC,ETH   --market-types crypto   --interval 60   --duration-minutes 60   --limit 100
```

Default database path:

```text
data/kalshi_market_data.db
```

Tables:

- `kalshi_market_snapshots`
- `kalshi_orderbook_snapshots`
- `kalshi_price_series`

Summarize collected data:

```bash
python -m src.cli kalshi-data-summary
```

The recorder stores timestamps, tickers, asset and market type classifications, yes/no bid/ask values, spreads, liquidity or volume when available, close/expiration times, and compact raw snapshots. It does not place or cancel orders.

Recorder discovery notes:

- `--limit` saves up to N matching markets after filtering.
- `--discovery-limit` controls how many raw Kalshi markets are inspected before filtering.
- `--ticker-prefix` and `--event-ticker-prefix` can target families such as `KXBTC`, `KXETH`, `KXSOL`, or `KXSOLANA`.

When asset filters include BTC, ETH, or SOL, discovery probes Kalshi `series_ticker` families such as `KXBTC`, `KXETH`, `KXSOL`, and `KXSOLANA` before falling back to broad pagination. Asset-filtered runs reject `KXMVE` multi-event bundle products.

## Market Data Backtesting

Run read-only backtests over `data/kalshi_market_data.db`:

```bash
python -m src.cli kalshi-backtest --strategy all --export
python -m src.cli kalshi-backtest-summary
```

Supported strategies:

- `spread-compression`
- `momentum`
- `mean-reversion`
- `probability-extremes`

Backtests use `kalshi_market_snapshots`, `kalshi_orderbook_snapshots`, and `kalshi_price_series` where available. Reports include PnL, fees, sharpe-like score, max drawdown, win rate, average hold time, and average spread captured. Export files:

```text
data/reports/kalshi_backtest.json
data/reports/kalshi_backtest.csv
```

This is read-only analysis and does not place or cancel orders.

## Live Smoke Test

Polylens includes one tightly gated live-order lifecycle smoke test. This is not strategy trading and does not enable general live trading.

The command refuses to run unless all gates pass:

- `LIVE_TRADING=true`
- `DRY_RUN=false`
- `KALSHI_LIVE_SMOKE_TEST=true`
- explicit `--ticker`
- explicit `--side yes|no`
- explicit `--price`
- explicit `--count`
- notional is less than or equal to `--max-notional` (default `$1.00`)

Manual command:

```bash
LIVE_TRADING=true DRY_RUN=false KALSHI_LIVE_SMOKE_TEST=true python -m src.cli kalshi-live-smoke-test   --ticker TICKER   --side yes   --price 1   --count 1   --max-notional 1
```

`--price 1` is interpreted as one cent (`0.01`). The smoke test creates a post-only resting order with a `polylens-smoke-...` client order id, verifies it exists, cancels it, and verifies cancellation. If any fill is detected, the command reports `partial_fill_detected` loudly.

Normal `place_order` and `cancel_order` methods remain blocked and return `write_blocked`.
