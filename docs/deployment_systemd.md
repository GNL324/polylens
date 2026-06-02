# Systemd Deployment

Polylens can run continuously on Predix with systemd and restart automatically after failures or reboot.

## Files

- `deploy/systemd/polylens-live-arb.service`
- `deploy/systemd/polylens-live-arb.env.example`
- `deploy/systemd/install_polylens_service.sh`
- `deploy/systemd/uninstall_polylens_service.sh`

The service runs from `/home/noel/polylens` using `/home/noel/.venv/bin/python`.

## Install

```bash
cd /home/noel/polylens
./deploy/systemd/install_polylens_service.sh
```

## Edit Env

```bash
nano /home/noel/polylens/deploy/systemd/polylens-live-arb.env
```

Set `ODDS_API_KEY`, `POLYLENS_WEBHOOK_URL`, thresholds, and `POLYLENS_DB_PATH` as needed.

## Enable And Start

```bash
sudo systemctl enable polylens-live-arb.service
sudo systemctl start polylens-live-arb.service
```

## Status And Logs

```bash
sudo systemctl status polylens-live-arb.service
journalctl -u polylens-live-arb.service -f
```

## Stop

```bash
sudo systemctl stop polylens-live-arb.service
```

## Update After Git Pull

```bash
cd /home/noel/polylens
git pull
source /home/noel/.venv/bin/activate
python -m pytest -q
sudo systemctl restart polylens-live-arb.service
```

## Uninstall

```bash
cd /home/noel/polylens
./deploy/systemd/uninstall_polylens_service.sh
```
