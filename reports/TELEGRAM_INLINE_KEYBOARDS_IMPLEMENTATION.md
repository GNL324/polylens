# Telegram Inline Keyboards Implementation

Branch: `feature/telegram-inline-keyboards`  
Base: `gitea/main`  
Worktree: `/home/noel/polylens-inline-keyboards`

## Summary

Implemented Telegram inline keyboard buttons for the Polylens Telegram control console. Slash commands now return optional Telegram `reply_markup`, `/start` and `/help` show a main menu, and callback button clicks route through the same safe command dispatch logic as text commands.

## Files Changed

- `src/integrations/telegram_console.py`
- `tests/test_telegram_console.py`
- `reports/TELEGRAM_INLINE_KEYBOARDS_IMPLEMENTATION.md`

## Inline Keyboard Menu

The main menu includes:

- Status
- Health
- Signals
- Top Wallets
- Paper Status
- Risk
- Help

Safe callback IDs:

- `status`
- `health`
- `signals`
- `top_wallets`
- `paper_status`
- `risk`
- `help`

`/wallet <address>` remains text-only.

## Safety Guarantees

- Read-only and paper-only scope is preserved.
- No live trading path was added.
- No wallet signing logic was added.
- No private-key handling was added.
- No Polymarket order placement path was added.
- Callback actions reuse the same command authorization and dispatch path as slash commands.
- Unauthorized callback users are rejected the same way as unauthorized slash command users.
- Missing admin allowlist still fails closed during startup validation and direct handling.
- Live-like callback IDs such as `kill_switch`, `buy`, `sell`, `order`, `trade`, and `resume_trading` are blocked with `live trading disabled`.
- Bot token is not logged; startup logging still uses the redacted safe config.
- Telegram send support serializes optional `reply_markup` without exposing the token.

## Audit Behavior

Callback actions are written to `telegram_command_audit` using `callback:<id>` in the `command` column.

Examples:

- `callback:health`
- `callback:unknown`
- `callback:kill_switch`

Audit rows continue to include:

- `timestamp_utc`
- `telegram_user_id`
- `command`
- `args`
- `allowed`
- `result_status`
- `error_message`

## Validation

```bash
PYTHONPATH=. .venv/bin/python -m pytest tests/test_telegram_console.py -q
```

Result:

```text
15 passed in 0.30s
```

```bash
PYTHONPATH=. .venv/bin/python -m pytest tests/test_telegram_console.py tests/test_systemd_deployment.py -q
```

Result:

```text
44 passed in 0.29s
```

```bash
PYTHONPATH=. .venv/bin/python -m pytest -q
```

Result:

```text
1080 passed in 510.34s (0:08:30)
```

## Remaining Work

- Consider editing callback responses in place via `editMessageText` in a later UI polish pass.
- Consider adding a text-only wallet lookup flow from a button in a later phase, while keeping wallet input explicit.
