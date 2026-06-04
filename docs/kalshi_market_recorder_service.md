# Kalshi Market Recorder Service

This service runs the Polylens Kalshi market data recorder continuously under systemd. It only reads public Kalshi market and orderbook data. It does not place or cancel orders.

## Files

```text
deploy/systemd/kalshi-market-recorder.service
deploy/systemd/kalshi-market-recorder.env.example
```

Create the local environment file:

```bash
cp deploy/systemd/kalshi-market-recorder.env.example deploy/systemd/kalshi-market-recorder.env
```

Default recorder command:

```bash
python -m src.cli kalshi-record-markets   --assets BTC,ETH,SOL   --market-types crypto   --interval 60   --limit 50   --discovery-limit 500   --db-path data/kalshi_market_data.db
```

## Install

```bash
sudo cp deploy/systemd/kalshi-market-recorder.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable kalshi-market-recorder
sudo systemctl start kalshi-market-recorder
systemctl status kalshi-market-recorder --no-pager
journalctl -u kalshi-market-recorder -n 100 --no-pager
```

## Stop Or Restart

```bash
sudo systemctl stop kalshi-market-recorder
sudo systemctl restart kalshi-market-recorder
```

## Update After Git Pull

```bash
cd /home/noel/polylens
source /home/noel/.venv/bin/activate
PYTHONPATH=. pytest -q
sudo cp deploy/systemd/kalshi-market-recorder.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl restart kalshi-market-recorder
journalctl -u kalshi-market-recorder -n 100 --no-pager
```

## Safety

- `LIVE_TRADING=false` and `DRY_RUN=true` are included in the example environment.
- The recorder uses read-only market/orderbook endpoints.
- Kalshi `place_order` and `cancel_order` remain blocked in code.
