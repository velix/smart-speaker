#!/usr/bin/env bash
# Pull-based deploy for the Pi. Run on the Pi from anywhere in the checkout:
#   ./scripts/pi-update.sh
set -euo pipefail
cd "$(dirname "$0")/.."

git pull --ff-only
uv sync

echo "Code updated."
if systemctl is-active --quiet voice-assistant 2>/dev/null; then
    echo "Restarting voice-assistant service..."
    sudo systemctl restart voice-assistant
fi
