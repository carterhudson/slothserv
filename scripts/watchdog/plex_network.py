"""
Keep Plex's customConnections in sync with the host's current LAN IP so
clients on the same network connect directly instead of falling back to
Plex's remote relay when the host's DHCP lease changes.
"""

import re
import subprocess
import time
from pathlib import Path

from watchdog import config
from watchdog.api import plex as plex_api

log = config.logger

PLEX_NETWORK_CHECK_INTERVAL = 5 * 60
PLEX_PREFS_REL = Path("config/plex/Library/Application Support/Plex Media Server/Preferences.xml")

last_network_check = 0.0

_CUSTOM_CONN_RE = re.compile(r'customConnections="([^"]*)"')
_LAN_URL_RE = re.compile(r"^https?://(?:192\.168|10\.|172\.(?:1[6-9]|2\d|3[01]))\.")


def _primary_lan_ip():
    try:
        route = subprocess.run(
            ["route", "get", "default"],
            capture_output=True, text=True, timeout=5,
        )
        iface = None
        for line in route.stdout.splitlines():
            line = line.strip()
            if line.startswith("interface:"):
                iface = line.split(":", 1)[1].strip()
                break
        if not iface:
            return None
        ip = subprocess.run(
            ["ipconfig", "getifaddr", iface],
            capture_output=True, text=True, timeout=5,
        ).stdout.strip()
        return ip or None
    except Exception:
        return None


def _plex_has_active_sessions():
    try:
        data = plex_api("/status/sessions") or {}
        mc = data.get("MediaContainer") or {}
        return int(mc.get("size", 0)) > 0
    except Exception:
        return False


def sync_custom_connections():
    global last_network_check
    now = time.time()
    if now - last_network_check < PLEX_NETWORK_CHECK_INTERVAL:
        return
    last_network_check = now

    prefs_path = config.BASE_DIR / PLEX_PREFS_REL
    if not prefs_path.exists():
        return

    ip = _primary_lan_ip()
    if not ip:
        log.debug("Plex network: could not resolve LAN IP")
        return

    text = prefs_path.read_text()
    m = _CUSTOM_CONN_RE.search(text)
    if not m:
        log.debug("Plex network: customConnections not set — skipping (configure via Plex UI first)")
        return

    existing = m.group(1)
    entries = [e for e in existing.split(",") if e]
    want = f"http://{ip}:32400"

    kept = [e for e in entries if not _LAN_URL_RE.match(e)]
    new_entries = kept + [want]

    if new_entries == entries:
        return

    log.info(f"Plex network: LAN IP drift detected ({existing} -> {','.join(new_entries)})")

    if _plex_has_active_sessions():
        last_network_check = now - PLEX_NETWORK_CHECK_INTERVAL + 60
        log.info("Plex network: active sessions present — deferring restart (retry in 1m)")
        return

    try:
        subprocess.run(
            ["docker", "compose", "stop", "plex"],
            cwd=str(config.BASE_DIR), capture_output=True, timeout=60, env=config.BREW_ENV,
        )
    except Exception as e:
        log.error(f"Plex network: failed to stop plex: {e}")
        return

    new_text = _CUSTOM_CONN_RE.sub(
        f'customConnections="{",".join(new_entries)}"', text,
    )
    prefs_path.write_text(new_text)

    try:
        subprocess.run(
            ["docker", "compose", "start", "plex"],
            cwd=str(config.BASE_DIR), capture_output=True, timeout=60, env=config.BREW_ENV,
        )
        log.info(f"Plex network: customConnections updated to include {want}")
    except Exception as e:
        log.error(f"Plex network: failed to start plex: {e}")
