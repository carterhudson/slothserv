#!/usr/bin/env bash
set -euo pipefail

BASE_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
PLIST="$HOME/Library/LaunchAgents/com.slothserv.watchdog.plist"

export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"

# ── Watchdog ─────────────────────────────────────────────────────
if launchctl list com.slothserv.watchdog &>/dev/null; then
  echo "Watchdog: unloading..."
  launchctl unload "$PLIST"
else
  echo "Watchdog: not loaded"
fi

# ── Rclone FUSE mount ───────────────────────────────────────────
echo "Rclone: stopping..."
docker compose -f "$BASE_DIR/docker-compose.yml" stop rclone 2>/dev/null || true
colima ssh -- sudo umount "$BASE_DIR/mnt/remote/nzbdav" 2>/dev/null || true

# ── Containers ──────────────────────────────────────────────────
echo "Containers: stopping..."
docker compose -f "$BASE_DIR/docker-compose.yml" --env-file "$BASE_DIR/.env" down

# ── Colima ──────────────────────────────────────────────────────
echo "Colima: stopping..."
colima stop

echo ""
echo "SlothServ is down."
