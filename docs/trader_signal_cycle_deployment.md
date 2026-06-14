# Trader Signal Cycle Deployment

This deployment runs the read-only, paper-only trader signal cycle on Predix.

## Scope

- Generates and scores trader signals from optional wallet activity JSON
- Updates optional read-only performance outcomes
- Produces paper-only recommendations and cycle metadata
- Does not place trades, send alerts, or enable live execution

## Files

- `deploy/systemd/polylens-trader-signal-cycle.service`
- `deploy/systemd/polylens-trader-signal-cycle.timer`
- `deploy/env/trader-signal-cycle.env.example`

## Install

```bash
cp deploy/env/trader-signal-cycle.env.example deploy/env/trader-signal-cycle.env
sudo cp deploy/systemd/polylens-trader-signal-cycle.service /etc/systemd/system/
sudo cp deploy/systemd/polylens-trader-signal-cycle.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now polylens-trader-signal-cycle.timer
```

## Manual run

```bash
cd /home/noel/polylens
PYTHONPATH=/home/noel/polylens python -m src.cli trader-signal-cycle --json
```

Optional inputs:

```bash
python -m src.cli trader-signal-cycle \
  --activity data/exports/wallet_activity.json \
  --outcomes data/exports/trader_signal_outcomes.json \
  --json
```

## Health check

```bash
python -m src.cli trader-signal-health --json
```

## Safety notes

- The systemd unit runs only `trader-signal-cycle --json`
- Live trading flags remain disabled in the unit and env example
- Default database path is `data/trader_signals.db`
