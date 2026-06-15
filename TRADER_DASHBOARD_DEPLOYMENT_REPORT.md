# Trader Dashboard Deployment Report

**Host:** Predix (`192.168.68.62`)  
**Repository:** https://git.noelgrca.com/Noel-Lab/polylens.git  
**Branch:** `main`  
**Deployment date:** 2026-06-15 UTC  

## Unit File

Created: `deploy/systemd/polylens-trader-dashboard.service`
Installed: `/etc/systemd/system/polylens-trader-dashboard.service`

```ini
[Unit]
Description=Polylens Trader Intelligence Center (read-only wallet analytics)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=noel
WorkingDirectory=/home/noel/polylens
Environment=PYTHONPATH=/home/noel/polylens
Environment=POLYLENS_LIVE_TRADING=false
Environment=POLYLENS_AUTONOMOUS_CRYPTO=false
Environment=POLYLENS_KALSHI_LIVE_SENDS_ENABLED=false
Environment=POLYLENS_POLYMARKET_LIVE_SENDS_ENABLED=false
ExecStart=/home/noel/.venv/bin/python -m src.cli trader-dashboard --host 127.0.0.1 --port 8788
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

## Deployment Steps

1. Created unit file in `/home/noel/polylens/deploy/systemd/`.
2. Copied to `/etc/systemd/system/` on Predix.
3. Ran `systemctl daemon-reload`.
4. Enabled and started with `systemctl enable --now polylens-trader-dashboard.service`.

## Service Status

```text
● polylens-trader-dashboard.service - Polylens Trader Intelligence Center (read-only wallet analytics)
     Loaded: loaded (/etc/systemd/system/polylens-trader-dashboard.service; enabled; preset: enabled)
     Active: active (running) since Mon 2026-06-15 18:01:07 UTC
   Main PID: 74449 (python)
      Tasks: 1
     Memory: 3.8M
        CPU: 6ms
```

## Listener Check

```text
LISTEN 0 2048 127.0.0.1:8788 0.0.0.0:* users:(("python",pid=74449,fd=19))
```

- ✅ Listening on `127.0.0.1:8788`
- ✅ No listener on `0.0.0.0:8788`

## curl Result

```text
http://127.0.0.1:8788/ → 200
```

Rendered navigation confirmed:
- Overview, Network, Profiles, Signals, Discovery, Performance, Service, Insights

## Safety Checks

| Check | Result |
|---|---|
| Binds only to `127.0.0.1` | ✅ Yes |
| No `0.0.0.0` listener | ✅ Yes |
| `POLYLENS_LIVE_TRADING=false` | ✅ Yes |
| `POLYLENS_AUTONOMOUS_CRYPTO=false` | ✅ Yes |
| `POLYLENS_KALSHI_LIVE_SENDS_ENABLED=false` | ✅ Yes |
| `POLYLENS_POLYMARKET_LIVE_SENDS_ENABLED=false` | ✅ Yes |
| No credentials in unit file | ✅ Yes |
| Service restarts on failure | ✅ Yes (`Restart=always`) |

## SRE Monitoring Update

The 30-minute SRE review job and daily operations report job have been updated to include:
- `systemctl is-active polylens-trader-dashboard.service`
- `curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8788/`
- Verification that `0.0.0.0:8788` is NOT listening
- Alert if the service is down, returns non-200, or is exposed on `0.0.0.0`

## Final Status

✅ `polylens-trader-dashboard.service` enabled and active  
✅ Dashboard reachable on `http://127.0.0.1:8788/`  
✅ Bound to localhost only  
✅ Live trading and autonomous crypto disabled  
✅ Included in SRE monitoring  

Access via SSH tunnel:

```bash
ssh -L 8788:127.0.0.1:8788 -N noel@predix
```

Then open `http://127.0.0.1:8788/` locally.
