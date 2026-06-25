# Telegram Paper Trading Intelligence Implementation

## Files Changed

- `src/analysis/paper_intelligence.py`
- `src/integrations/telegram_console.py`
- `src/integrations/telegram_notifications.py`
- `tests/test_telegram_console.py`
- `tests/test_telegram_notifications.py`
- `reports/TELEGRAM_PAPER_TRADING_INTELLIGENCE_IMPLEMENTATION.md`

## Data Sources Used

- `data/paper_trading.db`
  - `paper_positions`
  - `paper_orders`
  - `paper_settlements`
- `data/short_crypto_paper.db`, when present
  - `paper_trades`
  - `paper_settlements`

The provider opens SQLite databases with `mode=ro` and treats missing databases or missing paper tables as empty datasets.

## New Commands

- `/paper_recent`
- `/paper_pnl`
- `/paper_positions`
- `/paper_strategies`

Each command uses the same read-only paper intelligence provider and returns concise Telegram-safe text.

## Menu Changes

The Reports menu now includes:

- Daily Brief
- Signal Summary
- Wallet Summary
- Paper Performance
- Recent Paper Trades
- Paper PnL
- Open Positions
- Paper Strategies
- Back

Callbacks added:

- `paper_recent`
- `paper_pnl`
- `paper_positions`
- `paper_strategies`

Callbacks are routed through the existing Telegram console callback handler and audited as `callback:<id>`.

## Polymarket Analytics Wallet Links

Telegram responses that display valid EVM wallet addresses now include a read-only inline URL button:

- Button text: `View on Polymarket Analytics`
- URL format: `https://polymarketanalytics.com/traders/<wallet_address>`

Wallet link buttons are added for:

- `/wallet <address>`
- `/top_wallets`, for valid wallet rows
- Wallet Summary, when valid wallets are present
- Wallet promotion notifications
- Wallet discovery notifications
- Recent paper trade output, when a paper trade row includes a valid wallet address

Malformed wallet IDs are ignored and do not receive URL buttons.

## Daily Report Enhancement

The Paper Trading section now includes:

- Daily PnL
- 7-day PnL
- Total PnL
- Open positions count
- Closed positions count
- Win rate
- Recent paper trade summary, limited to 3
- Top strategy
- Worst strategy

## Safety Guarantees

- No live trading paths were modified.
- No Polymarket order placement was added.
- No wallet signing was added.
- No private key handling was added.
- No execution approvals were added.
- Polymarket Analytics buttons are URL-only and read-only.
- Wallet links are created only after strict `0x` EVM-style address validation.
- The new provider is read-only and paper-only.
- The provider does not call paper trading engine initialization helpers.
- The provider does not insert, update, delete, settle, or mutate paper trading rows.
- Tests cover that paper table row counts are unchanged after new Telegram paper commands.

## Test Results

- `PYTHONPATH=. .venv/bin/python -m pytest tests/test_telegram_console.py -q`
  - Superseded by the combined Telegram test run below after wallet-link tests were added.
- `PYTHONPATH=. .venv/bin/python -m pytest tests/test_telegram_notifications.py -q`
  - Superseded by the combined Telegram test run below after wallet-link tests were added.
- `PYTHONPATH=. /home/noel/polylens/.venv/bin/python -m pytest tests/test_telegram_console.py tests/test_telegram_notifications.py -q`
  - `52 passed in 0.79s`
- `PYTHONPATH=. .venv/bin/python -m pytest -q`
  - Hung late in the suite and was manually terminated after no new output for several minutes.
  - Bounded verbose isolation showed:
    - `tests/test_wallet_autonomy_service.py::test_run_due_cycles_records_service_state` hangs.
    - `tests/test_wallet_autonomy_cli.py::test_wallet_service_run_cli_force` hangs.
- `PYTHONPATH=. .venv/bin/python -m pytest -q -k 'not test_run_due_cycles_records_service_state and not test_wallet_service_run_cli_force'`
  - `1120 passed, 2 deselected in 45.20s`

The two hanging tests exercise wallet autonomy due-cycle execution and are outside the Telegram paper intelligence changes.

## Limitations / Missing Schema Fields

- `paper_orders` in `data/paper_trading.db` has no explicit fill timestamp, so recent simulated fills from that table use the available order metadata without a fill time.
- Open-position unrealized PnL depends on stored `unrealized_pnl`; the provider does not recompute marks.
- `data/short_crypto_paper.db` has no open-position table separate from `paper_trades`, so open positions are inferred from trades without settlements.
- Strategy names are normalized for known families and `btc_5m_momentum*`; unknown strategy labels are displayed as stored.
