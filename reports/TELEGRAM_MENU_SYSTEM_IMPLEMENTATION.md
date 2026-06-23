# Telegram Hierarchical Menu System Implementation

Branch: `feature/telegram-inline-keyboards`  
Worktree: `/home/noel/polylens-inline-keyboards`

## Summary

Implemented hierarchical Telegram inline menus for the Polylens Telegram control console. The bot now presents a category-based main menu, supports Back navigation, routes submenu actions through the existing callback authorization and audit flow, and edits existing Telegram messages whenever possible.

## Main Menu Categories

- Intelligence
- Wallets
- Signals
- System
- Reports

## Navigation

- Category buttons use safe callback IDs such as `menu_system` and `menu_wallets`.
- Submenus include a Back button using `menu_main`.
- Action buttons continue to use existing safe callback IDs such as `health`, `signals`, `top_wallets`, `paper_status`, and `risk`.
- `/wallet <address>` remains text-only.

## Telegram Message Behavior

- Callback responses try `editMessageText` first when Telegram provides a `message_id`.
- If editing fails, the console falls back to `sendMessage`.
- Plain slash command responses still use `sendMessage`.

## Safety Guarantees

- Read-only and paper-only behavior is preserved.
- No live trading path was added.
- No wallet signing logic was added.
- No private-key handling was added.
- No Polymarket order placement path was added.
- Callback authorization is reused for menu callbacks and action callbacks.
- Audit logging is reused for menu callbacks and action callbacks.
- Live-like callbacks remain blocked with `live trading disabled`.
- Bot token is not logged; edit fallback logging avoids printing exception text.
- Missing admin allowlist still fails closed.

## Audit Behavior

Menu callbacks are written to `telegram_command_audit` as `callback:<id>`.

Examples:

- `callback:menu_system`
- `callback:menu_main`
- `callback:health`

## Tests Added

- Menu navigation
- Back navigation
- Callback authorization
- Callback auditing
- Message editing when possible
- Message editing fallback to `sendMessage`

## Validation

```bash
PYTHONPATH=. .venv/bin/python -m pytest tests/test_telegram_console.py -q
```

Result:

```text
20 passed in 0.32s
```

```bash
PYTHONPATH=. .venv/bin/python -m pytest tests/test_telegram_console.py tests/test_systemd_deployment.py -q
```

Result:

```text
49 passed in 0.36s
```

```bash
PYTHONPATH=. .venv/bin/python -m pytest -q
```

Result:

```text
1085 passed in 567.39s (0:09:27)
```

## Remaining Work

- Consider adding richer read-only report pages under the Reports submenu.
- Consider adding a guided text prompt for `/wallet <address>` in a later phase without making wallet entry implicit.
