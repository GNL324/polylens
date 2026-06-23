# Telegram Callback Robustness Fix

Branch: `feature/telegram-inline-keyboards`  
Worktree: `/home/noel/polylens-inline-keyboards`

## Summary

Fixed callback query robustness so stale or expired Telegram callback acknowledgements cannot crash `poll_once`. Callback acknowledgement now runs through a tolerant helper, logs a token-safe warning, and processing continues for the current and later updates.

## Changes

- Added `_answer_callback_query`.
- Moved callback acknowledgement before edit/send handling.
- Wrapped `answerCallbackQuery` failures so HTTP 400 responses from stale callbacks are tolerated.
- Kept warning logs generic to avoid bot token leakage.
- Added regression coverage for `HTTPError` from `answerCallbackQuery`.

## Safety

- Read-only and paper-only behavior is unchanged.
- No live trading path was added.
- No wallet signing logic was added.
- No private-key handling was added.
- No order placement path was added.
- Callback authorization and audit behavior are unchanged.

## Validation

```bash
PYTHONPATH=. /home/noel/polylens/.venv/bin/python -m pytest tests/test_telegram_console.py -q
```

Result:

```text
21 passed in 0.35s
```

```bash
PYTHONPATH=. /home/noel/polylens/.venv/bin/python -m pytest tests/test_telegram_console.py tests/test_systemd_deployment.py -q
```

Result:

```text
50 passed in 0.44s
```
