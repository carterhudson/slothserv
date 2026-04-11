#!/usr/bin/env bash
set -euo pipefail

BASE_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
PLIST="$HOME/Library/LaunchAgents/com.slothserv.watchdog.plist"

export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"

# ── Colima ───────────────────────────────────────────────────────────
if colima status 2>/dev/null | grep -q Running; then
  echo "Colima: already running"
else
  echo "Colima: starting..."
  colima start
fi

# ── Docker Compose ───────────────────────────────────────────────────
echo "Containers: starting..."
docker compose -f "$BASE_DIR/docker-compose.yml" --env-file "$BASE_DIR/.env" up -d

# ── Watchdog ─────────────────────────────────────────────────────────
if launchctl list com.slothserv.watchdog &>/dev/null; then
  echo "Watchdog: already loaded"
else
  echo "Watchdog: loading..."
  launchctl load "$PLIST"
fi

echo ""
echo "SlothServ is up."
docker compose -f "$BASE_DIR/docker-compose.yml" ps --format 'table {{.Name}}\t{{.Status}}'
