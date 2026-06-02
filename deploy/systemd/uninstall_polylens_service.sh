#!/usr/bin/env bash
set -euo pipefail

SERVICE_NAME="polylens-live-arb.service"
SYSTEMD_PATH="/etc/systemd/system/$SERVICE_NAME"

sudo systemctl stop "$SERVICE_NAME" 2>/dev/null || true
sudo systemctl disable "$SERVICE_NAME" 2>/dev/null || true
sudo rm -f "$SYSTEMD_PATH"
sudo systemctl daemon-reload
echo "Uninstalled $SERVICE_NAME"
