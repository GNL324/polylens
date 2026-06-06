# Risk Engine

The risk engine is implemented in `src/risk` and stores state in SQLite, defaulting to `data/polylens.db`.

Safety defaults:

- `DRY_RUN=true`
- `LIVE_TRADING=false`
- Live execution remains blocked even if env flags are changed.
- No private keys are read or written by the risk engine.

Decisions:

- `APPROVE`: the opportunity or paper order is allowed.
- `REJECT`: the request is blocked for a risk-rule reason.
- `HALT`: global, venue, loss, or drawdown halt is active.
- `PAPER_ONLY`: live execution was requested, but Polylens allows only paper/dry-run handling.

Rules:

- Maximum daily loss
- Maximum monthly loss
- Maximum drawdown
- Maximum position size
- Maximum exposure per venue
- Maximum exposure per market
- Minimum opportunity score
- Minimum expected edge
- Duplicate opportunity suppression
- Global trading halt
- Venue-specific trading halt
- Dry-run/live safety check

SQLite tables:

- `risk_events`: every risk decision with timestamp, venue, market, opportunity ID, decision, reason, score, edge, proposed stake, current exposure, PnL state, and dry-run/live flags.
- `risk_state`: key/value state for daily PnL, monthly PnL, current equity, and peak equity.
- `position_exposure`: exposure deltas by venue and market.
- `trading_halts`: active and resumed global or venue-specific halts.

Useful commands:

```bash
python -m src.cli risk-status
python -m src.cli risk-events --limit 20
python -m src.cli risk-halt --reason "manual halt"
python -m src.cli risk-resume
```

Environment variables:

```text
RISK_MAX_DAILY_LOSS
RISK_MAX_MONTHLY_LOSS
RISK_MAX_DRAWDOWN
RISK_MAX_POSITION_SIZE
RISK_MAX_EXPOSURE_PER_VENUE
RISK_MAX_EXPOSURE_PER_MARKET
RISK_MIN_OPPORTUNITY_SCORE
RISK_MIN_EXPECTED_EDGE
RISK_DUPLICATE_OPPORTUNITY_COOLDOWN_SECONDS
DRY_RUN
LIVE_TRADING
```

Current integration points:

- Kalshi paper order submissions pass through the persistent risk engine before paper placement.
- Live Kalshi submission remains disabled and records a `PAPER_ONLY` risk decision when live mode is requested.
- Live arbitrage watch alerts pass through risk checks before notification.
- Prop arbitrage watch alerts pass through risk checks before Telegram notification.

Limitations:

- PnL values are placeholders until a real settlement/accounting feed updates `risk_state`.
- Exposure is updated for paper Kalshi orders; opportunity alerts do not increase exposure by default.
- The dashboard halt/resume buttons currently control global halts. Venue halts are available through the CLI/API.
