"""
Shared configuration, constants, mutable state, and logging setup.

Other modules import this module (not individual names) so mutations
to module-level variables are visible everywhere:

    from watchdog import config
    config.sonarr_url = "http://..."
"""

import logging
import logging.handlers
import os
from pathlib import Path

# ─── Paths ────────────────────────────────────────────────────────────

BASE_DIR = Path(__file__).resolve().parent.parent.parent   # media-server/
LOG_DIR = BASE_DIR / "logs"

# ─── Constants ────────────────────────────────────────────────────────

CHECK_INTERVAL = 5
EPISODE_SEARCH_DELAY = 2

ANIME_ROOT = "/data/media/anime"
TV_ROOT = "/data/media/tv"
ANIME_QUALITY_PROFILE_ID = 8
TV_QUALITY_PROFILE_ID = 7

MISSING_SWEEP_INTERVAL = 6 * 3600
BLOCKLIST_HYGIENE_INTERVAL = 4 * 3600
SYMLINK_RECONCILE_INTERVAL = 4 * 3600
DEAD_DOWNLOAD_GRACE = 5 * 60
SYMLINK_CLEANUP_INTERVAL = 4 * 3600
TRUNCATION_CHECK_INTERVAL = 6 * 3600
TRUNCATION_THRESHOLD = 0.6
HEALTH_CHECK_INTERVAL = 3600
VPN_CHECK_INTERVAL = 60
VPN_UNHEALTHY_THRESHOLD = 3

COLIMA_BIN = "/opt/homebrew/bin/colima"
BREW_ENV = {**os.environ, "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"}

# ─── Mutable state (written by load_api_keys / init_service_urls) ─────

sonarr_url = "http://localhost:8989"
radarr_url = "http://localhost:7878"
plex_url = "http://localhost:32400"
sonarr_api_key = ""
radarr_api_key = ""
plex_token = ""

force_health_check = False
start_time = 0.0

# ─── Logger ───────────────────────────────────────────────────────────

logger = logging.getLogger("watchdog")


def setup_logging():
    LOG_DIR.mkdir(exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        LOG_DIR / "watchdog.log", maxBytes=5_000_000, backupCount=3
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    logger.addHandler(logging.StreamHandler())
    logger.setLevel(logging.INFO)


def load_api_keys():
    global sonarr_api_key, radarr_api_key, plex_token

    sonarr_api_key = (BASE_DIR / "config/api-keys/sonarr.key").read_text().strip()

    try:
        radarr_api_key = (BASE_DIR / "config/api-keys/radarr.key").read_text().strip()
    except Exception:
        logger.warning("Could not read Radarr API key — Radarr integration disabled")

    try:
        plex_token = (BASE_DIR / "config/api-keys/plex.token").read_text().strip()
    except Exception:
        logger.warning("Could not read Plex token — session monitoring disabled")
