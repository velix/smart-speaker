#!/usr/bin/env bash
# Pull-based deploy for the Pi. Run on the Pi from anywhere in the checkout:
#   ./scripts/pi-update.sh
set -euo pipefail
cd "$(dirname "$0")/.."

# uv installs per-user (~/.local/bin), which non-interactive SSH sessions
# don't have on PATH — add it so `ssh pi-speaker './scripts/pi-update.sh'` works.
export PATH="$HOME/.local/bin:$PATH"

git pull --ff-only
uv sync

echo "Code updated."
if systemctl is-active --quiet voice-assistant 2>/dev/null; then
    echo "Restarting voice-assistant service..."
    sudo systemctl restart voice-assistant
fi
