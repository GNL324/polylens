# Short-Crypto Live Trading

This document describes the current state, safety gates, and operational procedures for Polylens short-window crypto trading on Kalshi and Polymarket.

Polylens defaults to paper mode and blocked live sends. Live execution requires explicit environment flags, a first-live-test run ID, funded accounts, and passing readiness audits.

## Current Status

| Area | Status |
|------|--------|
| Kalshi live path | Reaches authenticated `POST /portfolio/orders` when all gates pass and `POLYLENS_KALSHI_LIVE_SENDS_ENABLED=true` |
| Kalshi final blocker | **Insufficient account balance** — signing and payload validation succeed; Kalshi rejects or cannot fill when balance is too low for the minimum first-live order |
| Polymarket CLOB connectivity | **Works** — generic CLOB `/book` probes succeed via `clob_connectivity` mode (e.g. long-dated BTC price markets) |
| Polymarket short-crypto CLOB books | **Not ready** — BTC/ETH/SOL 5m/10m/15m up/down candidates are discovered in gamma, but CLOB `/book` currently returns **HTTP 404** for probed token IDs |
| Polymarket live sends | Blocked — signing may work with credentials, but non-short-crypto fallback markets are explicitly blocked from live send (`not_short_crypto_market`) |

Short-crypto readiness and live send require a **valid short-crypto CLOB book**. Generic Polymarket CLOB connectivity alone does not satisfy readiness.

## Safety Gates

All gates below must be understood before enabling live trading. Venue-specific send flags are separate and default off.

### Core live flags

| Variable | Purpose |
|----------|---------|
| `POLYLENS_FIRST_LIVE_TEST` | Enables first-live-test semantics: max one order, max ~$1 equivalent exposure, stricter payload validation |
| `POLYLENS_FIRST_LIVE_TEST_RUN_ID` | Required unique run ID included in dedupe key; prevents accidental replay of the same first-live attempt |
| `POLYLENS_LIVE_TRADING` | Master live-trading enable |
| `POLYLENS_AUTONOMOUS_CRYPTO` | Allows autonomous short-crypto execution (not manual one-shot only) |
| `POLYLENS_CONFIRM_RISK_ACK` | Explicit operator acknowledgment of live risk |

### Venue send flags

| Variable | Purpose |
|----------|---------|
| `POLYLENS_KALSHI_LIVE_SENDS_ENABLED` | Allows Kalshi authenticated order POST (default: off / blocked) |
| `POLYLENS_POLYMARKET_LIVE_SENDS_ENABLED` | Allows Polymarket signed order POST (default: off / blocked) |

### Risk limits (short-crypto)

| Variable | Default | Purpose |
|----------|---------|---------|
| `POLYLENS_SHORT_CRYPTO_MAX_STAKE` | `50` | Max stake per trade (USD notional basis for sizing) |
| `POLYLENS_SHORT_CRYPTO_MAX_STAKE_CAP` | `250` | Hard cap on configured max stake |
| `POLYLENS_SHORT_CRYPTO_MAX_DAILY_LOSS` | `250` | Stop trading after this daily loss |
| `POLYLENS_SHORT_CRYPTO_MAX_DAILY_NOTIONAL` | `250` | Max daily notional |
| `POLYLENS_SHORT_CRYPTO_MAX_OPEN_NOTIONAL` | `200` | Max open notional |
| `POLYLENS_SHORT_CRYPTO_MAX_TRADES_PER_DAY` | `10` | Max trades per day |
| `POLYLENS_SHORT_CRYPTO_MAX_PER_VENUE_NOTIONAL` | `150` | Per-venue notional cap |
| `POLYLENS_SHORT_CRYPTO_MIN_EDGE` | `0.01` | Minimum edge to accept a signal |
| `POLYLENS_SHORT_CRYPTO_MIN_LIQUIDITY` | `1.0` | Minimum book liquidity |
| `POLYLENS_SHORT_CRYPTO_DEDUPE` | `true` | Duplicate trade protection |
| `POLYLENS_KILL_SWITCH` | `/home/noel/polylens/.kill_switch` | Path to kill-switch file |

### Credentials

**Kalshi:** `KALSHI_API_KEY_ID`, `KALSHI_PRIVATE_KEY_PATH` (or inline key per adapter config)

**Polymarket:** `POLYMARKET_PRIVATE_KEY`, `POLYMARKET_API_KEY`, `POLYMARKET_API_SECRET`, `POLYMARKET_API_PASSPHRASE`, optional `POLYMARKET_FUNDER`, `POLYMARKET_SIGNATURE_TYPE`

Never commit credentials. Use `secrets/` or systemd env files outside git.

## First-Live-Test Procedure (Kalshi)

Use this sequence for a single controlled live order on Kalshi short-crypto markets.

### 1. Preflight (no sends)

```bash
cd /home/noel/polylens

# Overall short-crypto readiness (Kalshi books, Coinbase feed, risk config)
PYTHONPATH=. .venv/bin/python -m src.cli live-readiness-short-crypto --json

# Kalshi BTC candidate selection, payload build, gate validation (sent=false)
PYTHONPATH=. .venv/bin/python live_order_audit.py

# Optional: inspect a specific ticker ladder
PYTHONPATH=. .venv/bin/python kalshi_orderbook_audit.py KXBTCD-...

# Optional: compare Kalshi order semantics (buy yes vs sell no into NO bid)
PYTHONPATH=. .venv/bin/python kalshi_order_semantics_audit.py KXBTCD-...
```

Confirm:

- `live_order_audit.py` reports `status: ready` for signing and payload validation
- `sent: false` in audit output
- Account balance sufficient for at least one minimum first-live contract (see [Funding requirements](#funding-requirements))

### 2. Set environment

```bash
export POLYLENS_FIRST_LIVE_TEST=true
export POLYLENS_FIRST_LIVE_TEST_RUN_ID="first-live-YYYYMMDD-HHMM"   # unique per attempt
export POLYLENS_LIVE_TRADING=true
export POLYLENS_AUTONOMOUS_CRYPTO=true
export POLYLENS_CONFIRM_RISK_ACK=true
export POLYLENS_KALSHI_LIVE_SENDS_ENABLED=true
# Do NOT set POLYLENS_POLYMARKET_LIVE_SENDS_ENABLED unless Polymarket short-crypto is ready
```

Ensure kill switch is **absent**:

```bash
test ! -f /home/noel/polylens/.kill_switch && echo "kill switch clear"
```

### 3. Dry-run live payload

```bash
PYTHONPATH=. .venv/bin/python -m src.cli trade-short-crypto \
  --venue kalshi --assets BTC --windows 5 \
  --dry-run-live --json
```

Review the built Kalshi payload and rejection reasons. No order should be sent.

### 4. Execute single live order

```bash
PYTHONPATH=. .venv/bin/python -m src.cli trade-short-crypto \
  --venue kalshi --assets BTC --windows 5 \
  --live --max-loops 1 --json
```

First-live-test enforces:

- Exactly **one** order (`max-loops 1`, count 1)
- Max equivalent exposure **≤ $1**
- Dedupe key includes `POLYLENS_FIRST_LIVE_TEST_RUN_ID`

### 5. Post-trade

- Verify fill in Kalshi UI or `python -m src.cli kalshi-orders --json`
- Do not reuse the same `POLYLENS_FIRST_LIVE_TEST_RUN_ID`
- Leave `POLYLENS_KALSHI_LIVE_SENDS_ENABLED` unset/false until intentionally re-enabled

## Polymarket Readiness Procedure

Polymarket readiness is split from generic CLOB connectivity.

### Short-crypto readiness (required for live)

```bash
PYTHONPATH=. .venv/bin/python polymarket_live_order_audit.py --mode short_crypto
PYTHONPATH=. .venv/bin/python -m src.cli live-readiness-polymarket --json
```

Expect `polymarket_short_crypto_clob_book.ok: false` while short-crypto token books return 404.

Output includes:

- `candidates_checked` — probed BTC/ETH/SOL 5m/10m/15m up/down markets
- `clob_book_404_token_ids` — token IDs with no CLOB book
- `reason: no_short_crypto_clob_book_available` when all probes fail

### CLOB connectivity only (informational)

```bash
PYTHONPATH=. .venv/bin/python polymarket_live_order_audit.py --mode clob_connectivity
```

May fall back to a liquid long-dated market (e.g. `will-bitcoin-hit-150k-by-june-30-2026`) with:

- `not_short_crypto: true`
- `purpose: clob_connectivity_only`

This confirms Polymarket API/CLOB reachability but **does not** authorize short-crypto live send. Fallback markets add `not_short_crypto_market` to failed gates and set `live_send_allowed: false`.

### Polymarket dry-run (when short-crypto book exists)

```bash
PYTHONPATH=. .venv/bin/python -m src.cli trade-short-crypto \
  --venue polymarket --assets BTC --windows 5 \
  --dry-run-live --json
```

Uses `build_audit(mode="short_crypto")` only.

## Kill Switch

Create the kill-switch file to halt all short-crypto execution immediately:

```bash
touch /home/noel/polylens/.kill_switch
```

Or set `POLYLENS_KILL_SWITCH` to a custom path and create that file.

Remove the file to resume (after verifying gates and readiness):

```bash
rm /home/noel/polylens/.kill_switch
```

Readiness checks report `kill_switch_absent: false` when active.

CLI risk commands (see [docs/risk_engine.md](risk_engine.md)):

```bash
python -m src.cli risk-halt --reason "manual halt"
python -m src.cli risk-status --json
python -m src.cli risk-resume
```

## Duplicate Trade Protection

When `POLYLENS_SHORT_CRYPTO_DEDUPE=true` (default):

- Trade keys are hashed from venue, ticker, side, and time window
- First-live-test keys additionally include `POLYLENS_FIRST_LIVE_TEST_RUN_ID`
- Polymarket first-live keys use `polymarket|{market_slug}|{token_id}|{run_id}`

Repeating the same run ID or trade key returns `duplicate_trade_key` and blocks send.

Tables: `data/polylens.db` (executor dedupe), `polymarket_first_live_keys` (Polymarket audit).

## Funding Requirements

### Kalshi

- First-live-test targets **1 contract** with **≤ $1** equivalent exposure (often 1 cent × 1 contract on selected ladder)
- Account must have sufficient **available balance** for the order plus fees; current milestone blocker is insufficient balance on the funded Kalshi account
- Check balance: `python -m src.cli kalshi-balance --json`

### Polymarket

- Requires funded proxy wallet / funder address with USDC allowance on Polygon
- Balance/allowance checked in `polymarket_live_order_audit.py` when credentials present
- Short-crypto live not enabled until CLOB books exist for short-window token IDs

## Known Limitations

1. **Polymarket short-crypto CLOB 404s** — gamma lists up/down markets but CLOB `/book` returns 404 for current token IDs; live Polymarket short-crypto is blocked until books exist.
2. **No Polymarket live send on fallback** — long-dated connectivity markets cannot be used for live short-crypto orders.
3. **Kalshi ladder semantics** — executable YES ask is often derived from best NO bid (`sell no` into NO bid ladder); use `kalshi_order_semantics_audit.py` to inspect payloads.
4. **First-live-test only** — production-sized loops require all live flags without first-live caps; not validated in this milestone.
5. **Polymarket signing** — requires `py_clob_client` / `py_clob_client_v2` SDK installed in `.venv`.
6. **10m/15m windows** — discovery iterates 5/10/15m; active gamma listings may only include 5m at a given time.
7. **README disclaimer** — general Polylens prop/arb tooling remains research-oriented; short-crypto live is opt-in and gate-heavy.

## Audit and Readiness Commands

Run from repo root `/home/noel/polylens`:

```bash
# Kalshi live order audit (BTC selection, payload, gates; sent=false)
PYTHONPATH=. .venv/bin/python live_order_audit.py

# Kalshi orderbook ladder inspection for one ticker
PYTHONPATH=. .venv/bin/python kalshi_orderbook_audit.py <TICKER>

# Kalshi buy/sell semantics and candidate payloads (no send)
PYTHONPATH=. .venv/bin/python kalshi_order_semantics_audit.py <TICKER>

# Polymarket short-crypto discovery only (no long-dated fallback)
PYTHONPATH=. .venv/bin/python polymarket_live_order_audit.py --mode short_crypto

# Polymarket generic CLOB connectivity (fallback allowed)
PYTHONPATH=. .venv/bin/python polymarket_live_order_audit.py --mode clob_connectivity

# Polymarket readiness JSON (short-crypto gates + connectivity info)
PYTHONPATH=. .venv/bin/python -m src.cli live-readiness-polymarket --json

# Kalshi + feed + risk readiness JSON
PYTHONPATH=. .venv/bin/python -m src.cli live-readiness-short-crypto --json
```

## Related Documentation

- [risk_engine.md](risk_engine.md) — halt/resume and risk events
- [kalshi_adapter.md](kalshi_adapter.md) — Kalshi API adapter
- [kalshi_automation.md](kalshi_automation.md) — account reads and reporting
- [architecture.md](architecture.md) — module map
