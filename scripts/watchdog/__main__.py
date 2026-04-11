"""
Watchdog daemon entry point.

Run as:
    python3 /path/to/scripts/watchdog        (launchd)
    python3 -m watchdog                       (from scripts/)
"""

import sys
from pathlib import Path

# Ensure the parent of watchdog/ (i.e. scripts/) is on sys.path so
# `from watchdog import ...` resolves to this package regardless of
# how the script is invoked (python3 -m watchdog vs python3 path/to/watchdog).
_scripts_dir = str(Path(__file__).resolve().parent.parent)
if _scripts_dir not in sys.path:
    sys.path.insert(0, _scripts_dir)

import time

from watchdog import config
from watchdog.connectivity import init_service_urls, check_connectivity
from watchdog.sonarr import (
    sync_watchlist,
    snapshot_series,
    detect_and_search_new_series,
    handle_stuck_imports as sonarr_stuck_imports,
    handle_failed_downloads as sonarr_failed_downloads,
    blocklist_hygiene,
    sweep_missing_episodes,
    reconcile_anime_symlinks,
)
from watchdog import radarr
from watchdog.plex import detect_truncated_episodes
from watchdog.health import health_check
from watchdog.vpn import check_health as check_vpn_health
from watchdog.nzbdav import check_article_health
from watchdog.backup import backup_configs


def _safe(name, fn):
    """Run a loop step in isolation. Logs any failure with a full traceback."""
    try:
        fn()
    except Exception:
        config.logger.exception(f"{name} failed")


def main():
    config.setup_logging()
    config.load_api_keys()
    config.logger.info("Watchdog started")

    init_service_urls()

    try:
        snapshot_series()
    except Exception:
        config.logger.exception("Failed to snapshot series")

    # Ordering note: sync_watchlist sets config.force_health_check on a 500,
    # so health_check must run AFTER it to act on the flag this cycle.
    steps = [
        ("check_connectivity", check_connectivity),
        ("check_vpn_health", check_vpn_health),
        ("sync_watchlist", sync_watchlist),
        ("health_check", health_check),
        ("detect_and_search_new_series", detect_and_search_new_series),
        ("sonarr_stuck_imports", sonarr_stuck_imports),
        ("sonarr_failed_downloads", sonarr_failed_downloads),
        ("radarr_stuck_imports", radarr.handle_stuck_imports),
        ("radarr_failed_downloads", radarr.handle_failed_downloads),
        ("blocklist_hygiene", blocklist_hygiene),
        ("sweep_missing_episodes", sweep_missing_episodes),
        ("reconcile_anime_symlinks", reconcile_anime_symlinks),
        ("detect_truncated_episodes", detect_truncated_episodes),
        ("check_article_health", check_article_health),
        ("backup_configs", backup_configs),
    ]

    while True:
        for name, fn in steps:
            _safe(name, fn)
        time.sleep(config.CHECK_INTERVAL)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        config.logger.info("Watchdog stopped")
