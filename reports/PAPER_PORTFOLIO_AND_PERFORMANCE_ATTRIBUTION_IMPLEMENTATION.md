# Paper Portfolio And Performance Attribution Implementation

## Summary

Implemented a read-only analytics layer on top of the existing paper trading engine. The paper engine now records portfolio ledger events, balance snapshots, and completed-trade attribution after simulated paper opens, simulated paper closes, and equity snapshots. No live trading paths, wallet signing, Polymarket order placement, private keys, or execution approvals were added.

## Schema Additions

New tables in `data/paper_trading.db`:

- `paper_portfolio_ledger`: canonical simulated portfolio event history with timestamp, trade ID, strategy, wallet, market, side, action, quantity, entry and exit price, realized and unrealized PnL, fees, cash before and after, portfolio value, buying power, position size, notes, and raw metadata.
- `paper_balance_snapshots`: timestamped cash, invested capital, unrealized PnL, realized PnL, total equity, drawdown, exposure, open positions, and closed positions.
- `paper_trade_attribution`: completed paper trade attribution with gross profit, gross loss, net PnL, duration, exit reason, market resolution, confidence score, strategy, wallet, signal family, ROI, and notional.

## Files Changed

- `src/analysis/paper_portfolio.py`
- `src/analysis/paper_trading_engine.py`
- `src/integrations/telegram_console.py`
- `src/integrations/telegram_notifications.py`
- `src/web/dashboard.py`
- `tests/test_paper_portfolio.py`
- `tests/test_paper_trading_engine.py`
- `tests/test_telegram_console.py`
- `tests/test_telegram_notifications.py`
- `tests/test_web_dashboard_cli.py`
- `reports/PAPER_PORTFOLIO_AND_PERFORMANCE_ATTRIBUTION_IMPLEMENTATION.md`

## Data Sources Used

- Existing paper trading tables: `paper_orders`, `paper_positions`, `paper_settlements`, and `paper_equity_curve`.
- Existing paper opportunity raw JSON metadata for wallet, signal family, and confidence where present.
- New paper-only analytics tables listed above.

## New Provider Functions

- `init_paper_portfolio_db`
- `record_position_opened`
- `record_position_closed`
- `record_balance_snapshot`
- `rebuild_portfolio_analytics`
- `portfolio_report`
- `trade_detail`
- `wallet_attribution`
- `strategy_attribution`
- `reconstruct_portfolio_value_at`
- `replay_portfolio`
- `polymarket_analytics_url`

## New Telegram Commands

- `/portfolio`
- `/history`
- `/equity`
- `/trade <id>`
- `/wallet_stats <wallet>`
- `/strategy_stats`
- `/top_winners`
- `/top_losers`

The Reports menu now links to portfolio, trade history, equity curve, and strategy stats. Existing Telegram audit behavior records the new commands and callbacks.

## Daily Report Additions

The Paper Trading section now includes portfolio value, cash, equity, buying power, open and closed positions, daily PnL, 7 day PnL, 30 day PnL, all-time PnL, largest winner, largest loser, best/worst wallet, best/worst strategy, current drawdown, and current exposure.

## Dashboard Additions

The dashboard Results / P&L page now exposes read-only paper portfolio views:

- Paper Portfolio
- Equity Curve
- Trade History
- Wallet Performance
- Strategy Performance
- PnL Attribution
- Capital Allocation
- Recent Trades

## Wallet Links

Wallet attribution validates `0x` EVM-style wallet addresses before generating Polymarket Analytics URLs:

`https://polymarketanalytics.com/traders/<wallet>`

Malformed wallet IDs do not receive links.

## Safety Guarantees

- No live trading was enabled.
- No Polymarket order placement was added.
- No signing, private keys, or execution approvals were added.
- Existing paper execution decisions, scoring, promotion, trading, signing, keys, order placement, and live-trading flags were not modified.
- Analytics writes happen only after existing simulated paper events or from explicit analytics rebuild/snapshot calls.
- Reporting and dashboard reads are read-only against existing paper trading state, aside from initializing missing analytics tables.

## Performance Considerations

- Attribution and report queries aggregate from local SQLite tables.
- Ledger inserts use `INSERT OR IGNORE` uniqueness on `(event_type, paper_position_id)` to avoid duplicate open/close events.
- Balance snapshots are append-only so portfolio value can be reconstructed over time.
- Report output limits recent ledger/trade rows to keep Telegram and dashboard rendering bounded.

## Examples

`/portfolio`:

```text
Paper Portfolio
Cash: +$99.00
Equity: +$101.00
Buying Power: +$99.00
Invested: +$2.00
Open: 1
Closed: 1
Today: +$1.00
7D: +$1.00
30D: +$1.00
All time: +$1.00
```

`/trade 1`:

```text
Paper Trade #1
Status: CLOSED
PnL: +$1.00
Duration: 1h
Wallet: 0x7af3f727e86394ca3986a1f786b888c7904e83fe
Strategy: early_entry
Market: Will BTC close above 100k?
Exit: simulated_exit
```

## Validation

Passed:

```bash
PYTHONPATH=. /home/noel/polylens/.venv/bin/python -m pytest tests/test_paper*.py -q
```

Result: `51 passed in 3.35s`

Passed:

```bash
PYTHONPATH=. /home/noel/polylens/.venv/bin/python -m pytest tests/test_telegram_console.py tests/test_telegram_notifications.py -q
```

Result: `75 passed in 1.18s`

Full requested suite was attempted with a 300 second guard:

```bash
timeout 300s env PYTHONPATH=. /home/noel/polylens/.venv/bin/python -m pytest -q
```

Result: reached approximately 87 percent then timed out without a final pytest summary.

Repository suite excluding the two known hanging wallet autonomy tests passed:

```bash
PYTHONPATH=. /home/noel/polylens/.venv/bin/python -m pytest -q -k 'not test_run_due_cycles_records_service_state and not test_wallet_service_run_cli_force'
```

Result: `1150 passed, 2 deselected in 42.96s`

The two deselected tests were isolated with a 60 second guard:

```bash
timeout 60s env PYTHONPATH=. /home/noel/polylens/.venv/bin/python -m pytest tests/test_wallet_autonomy_service.py::test_run_due_cycles_records_service_state tests/test_wallet_autonomy_cli.py::test_wallet_service_run_cli_force -q
```

Result: timed out without output.

## Limitations

- Historical paper positions can be rebuilt into attribution records, but ledger rows are canonical only once generated by the analytics recording hooks or explicit rebuild.
- Fees are currently recorded as zero because the existing simulated paper schema does not expose a fee field.
- Market resolution is inferred from settlement reason or realized PnL when no explicit resolution field exists.
- Wallet, signal family, and confidence attribution depend on the fields available in `paper_orders.raw_json`.
- No migration framework file was added; schema initialization follows the existing paper trading SQLite initialization pattern.
