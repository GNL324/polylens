#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="/home/noel/polylens"
SERVICE_NAME="polylens-live-arb.service"
SYSTEMD_DIR="/etc/systemd/system"
SERVICE_SRC="$PROJECT_ROOT/deploy/systemd/$SERVICE_NAME"
ENV_EXAMPLE="$PROJECT_ROOT/deploy/systemd/polylens-live-arb.env.example"
ENV_FILE="$PROJECT_ROOT/deploy/systemd/polylens-live-arb.env"

if [[ ! -f "$ENV_FILE" ]]; then
  cp "$ENV_EXAMPLE" "$ENV_FILE"
  echo "Created $ENV_FILE. Edit it before starting the service."
fi

sudo cp "$SERVICE_SRC" "$SYSTEMD_DIR/$SERVICE_NAME"
sudo systemctl daemon-reload
echo "Installed $SERVICE_NAME"
echo "Enable with: sudo systemctl enable $SERVICE_NAME"
echo "Start with:  sudo systemctl start $SERVICE_NAME"
