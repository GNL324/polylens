# Telegram-First SRE Report Migration

## Summary

Polylens wallet autonomy SRE reporting is now available through the Telegram operational console and optional proactive Telegram alerts. The Hermes cronjob (`polylens-wallet-autonomy-sre-review`) is **not removed** in this change; cut over only after Telegram delivery is verified.

## Surfaces

### On-demand: Telegram Console → System → Ops Health

- Menu path: **Home › System › 🛡️ Ops Health**
- Callback: `quick_sre_status`
- Renders read-only status from `wallet_autonomy_sre_check` logic (`src/integrations/sre_health.py`)

Displayed fields:

- Overall status (HEALTHY / ALERT)
- Alert and warning counts
- Alert codes and messages
- Deployment drift status
- Wallet service health status
- Recommended actions
- Last checked timestamp

Healthy reports are **on-demand only** (not pushed).

### Proactive alerts: systemd timer + CLI

- CLI: `python -m src.cli telegram-sre-alert`
- Timer: `polylens-telegram-sre-alert.timer` (every 15 minutes)
- Dedupe state: `data/sre_telegram_alert_state.json` (override with `POLYLENS_SRE_ALERT_STATE_PATH`)

Alert policy:

- Send when status is **alert** or there are **warnings**
- Send when status **changes** (including recovery from alert → healthy)
- Suppress repeated identical alert fingerprints
- Do **not** spam healthy all-clear on every timer tick

Enable/disable proactive delivery:

```bash
POLYLENS_TELEGRAM_SRE_ALERT_ENABLED=true   # default
POLYLENS_TELEGRAM_SRE_ALERT_ENABLED=false  # evaluate only, no Telegram send
```

## CLI commands

```bash
# On-demand check (stdout); exit 2 when alerts present
python -m src.cli telegram-sre-check
python -m src.cli telegram-sre-check --json

# Proactive alert sender (used by systemd timer)
python -m src.cli telegram-sre-alert --dry-run --json
python -m src.cli telegram-sre-alert --json
```

Legacy script still works:

```bash
python scripts/wallet_autonomy_sre_check.py --json
```

## systemd install (after verification)

```bash
sudo cp deploy/systemd/polylens-telegram-sre-alert.service deploy/systemd/polylens-telegram-sre-alert.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now polylens-telegram-sre-alert.timer
systemctl list-timers polylens-telegram-sre-alert.timer
```

Requires `deploy/systemd/polylens-telegram-console.env` with bot token and admin chat id (same as daily report).

## Hermes cutover (manual, after Telegram verified)

1. Confirm Telegram proactive alerts fire on injected alert/warning and suppress duplicates.
2. Confirm Ops Health page renders correctly from the console bot.
3. Enable `polylens-telegram-sre-alert.timer`.
4. Disable Hermes reminder:

```text
stop reminder polylens-wallet-autonomy-sre-review
```

Do **not** disable Hermes until steps 1–3 pass in production.

## Safety

- Read-only checks only
- Paper-only; no live trading, order placement, or wallet signing
- Telegram remains admin-allowlisted via existing console configuration
- No credential exposure in report payloads
