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
