"""
Health checks: container liveness and import list staleness.

Note: there is no DB-integrity repair anymore. Sonarr/Radarr live on
named Docker volumes (ext4 inside the Colima VM), which eliminated the
virtiofs + WAL corruption that the repair path used to clean up.
"""

import subprocess
import time

from watchdog import config
from watchdog.api import sonarr as sonarr_api, radarr as radarr_api, plex_watchlist

log = config.logger

# ─── Module state ─────────────────────────────────────────────────────

last_health_check = 0.0

# Track when a GUID was first seen as "on watchlist but missing from library"
# so we don't thrash the import list on items that Sonarr just hasn't
# ingested yet.
_missing_since: dict = {"sonarr": {}, "radarr": {}}

# Only recreate an import list if a watchlist item has been missing this
# long. Gives ImportListSync time to pick it up on its own.
IMPORT_LIST_RECREATE_DELAY = 4 * 3600

CRITICAL_CONTAINERS = ["sonarr", "radarr", "plex", "rclone"]
OPTIONAL_CONTAINERS = ["gluetun"]


# ─── Public entry point ──────────────────────────────────────────────

def health_check():
    """
    Hourly health check (or on-demand after a 500 error) that catches:
    1. Containers not running
    2. Stale import lists (watchlist items not making it into the library)
    """
    global last_health_check
    now = time.time()
    if not config.force_health_check and now - last_health_check < config.HEALTH_CHECK_INTERVAL:
        return
    last_health_check = now
    config.force_health_check = False

    log.info("Health check: containers, import lists")

    _check_containers()
    _verify_import_lists()


# ─── Container liveness ─────────────────────────────────────────────

def _check_containers():
    """Verify critical containers are running; restart any that aren't.
    Containers that don't exist (e.g. optional gluetun when not deployed) are skipped."""
    for name in CRITICAL_CONTAINERS + OPTIONAL_CONTAINERS:
        try:
            result = subprocess.run(
                ["docker", "inspect", "--format", "{{.State.Status}}", name],
                capture_output=True, text=True, timeout=10, env=config.BREW_ENV,
            )
        except Exception:
            continue

        if result.returncode != 0:
            continue

        status = result.stdout.strip()
        if status == "running":
            continue

        if name in CRITICAL_CONTAINERS:
            log.error(f"  Container '{name}' is {status} — attempting restart")
        else:
            log.warning(f"  Container '{name}' is {status} — attempting restart")

        try:
            subprocess.run(
                ["docker", "compose", "up", "-d", name],
                cwd=str(config.BASE_DIR), capture_output=True, timeout=60,
                env=config.BREW_ENV,
            )
            time.sleep(10)
            verify = subprocess.run(
                ["docker", "inspect", "--format", "{{.State.Status}}", name],
                capture_output=True, text=True, timeout=10, env=config.BREW_ENV,
            )
            if verify.stdout.strip() == "running":
                log.info(f"  Container '{name}' restarted successfully")
            else:
                log.error(f"  Container '{name}' failed to restart")
        except Exception as e:
            log.error(f"  Failed to restart container '{name}': {e}")


# ─── Import list staleness ────────────────────────────────────────────

def _verify_import_lists():
    """
    Compare Plex Watchlist (via GUID matching) with Sonarr/Radarr libraries.
    Items on the watchlist but not in the library get an ImportListSync nudge.
    If they remain missing past IMPORT_LIST_RECREATE_DELAY, the import list
    is deleted and recreated with a fresh token.
    """
    if not config.plex_token:
        return

    try:
        items = plex_watchlist()
    except Exception as e:
        log.debug(f"  Could not fetch Plex Watchlist: {e}")
        return

    if items is None:
        return

    if not items:
        log.info("  Plex Watchlist empty — nothing to verify")
        _missing_since["sonarr"].clear()
        _missing_since["radarr"].clear()
        return

    watchlist_tvdb: dict = {}
    watchlist_tmdb: dict = {}

    for item in items:
        title = item.get("title", "?")
        for guid in item.get("Guid") or []:
            gid = guid.get("id", "")
            if gid.startswith("tvdb://"):
                try:
                    watchlist_tvdb[int(gid[7:])] = title
                except ValueError:
                    pass
            elif gid.startswith("tmdb://"):
                try:
                    watchlist_tmdb[int(gid[7:])] = title
                except ValueError:
                    pass

    if watchlist_tvdb:
        _verify_service_guids("sonarr", watchlist_tvdb, "tvdbId")
    if watchlist_tmdb and config.radarr_api_key:
        _verify_service_guids("radarr", watchlist_tmdb, "tmdbId")


def _verify_service_guids(service, watchlist_ids, id_field):
    """
    watchlist_ids: {external_id: title}
    id_field: "tvdbId" or "tmdbId"
    """
    call = sonarr_api if service == "sonarr" else radarr_api
    endpoint = "/api/v3/series" if service == "sonarr" else "/api/v3/movie"

    try:
        library = call("GET", endpoint) or []
    except Exception as e:
        log.error(f"  {service} library fetch failed: {e}")
        return

    library_ids = {item.get(id_field) for item in library if item.get(id_field)}
    missing_ids = {eid: title for eid, title in watchlist_ids.items() if eid not in library_ids}

    now = time.time()
    tracked = _missing_since[service]

    for eid in list(tracked.keys()):
        if eid not in missing_ids:
            del tracked[eid]

    if not missing_ids:
        return

    for eid in missing_ids:
        tracked.setdefault(eid, now)

    persistent = [eid for eid, t in tracked.items() if now - t >= IMPORT_LIST_RECREATE_DELAY]
    if persistent:
        persistent_titles = sorted(missing_ids[eid] for eid in persistent if eid in missing_ids)
        hours = IMPORT_LIST_RECREATE_DELAY // 3600
        log.warning(
            f"  {service}: {len(persistent_titles)} watchlist item(s) missing >{hours}h: "
            f"{', '.join(persistent_titles[:5])}"
        )
        _recreate_import_list(service)
        tracked.clear()
    else:
        missing_titles = sorted(missing_ids.values())
        log.info(
            f"  {service}: {len(missing_titles)} watchlist item(s) not yet imported — "
            f"triggering ImportListSync: {', '.join(missing_titles[:5])}"
        )
        try:
            call("POST", "/api/v3/command", {"name": "ImportListSync"})
        except Exception as e:
            log.debug(f"  {service} ImportListSync failed: {e}")


def _recreate_import_list(service):
    """Delete and recreate the Plex import list with the current token."""
    log.info(f"  Recreating {service} import list to clear stale cache")

    call = sonarr_api if service == "sonarr" else radarr_api

    try:
        lists = call("GET", "/api/v3/importlist") or []

        plex_lists = [il for il in lists if il.get("implementation") == "PlexImport"]
        if not plex_lists:
            log.warning(f"  No Plex import list found in {service}")
            return

        for il in plex_lists:
            il_config = dict(il)
            old_id = il_config.pop("id", None)

            for field in il_config.get("fields", []):
                if field["name"] == "accessToken":
                    field["value"] = config.plex_token

            call("DELETE", f"/api/v3/importlist/{old_id}")
            result = call("POST", "/api/v3/importlist", il_config)

            new_id = (result or {}).get("id", "?")
            log.info(f"  Replaced import list {old_id} → {new_id} with fresh token")

    except Exception as e:
        log.error(f"  Failed to recreate {service} import list: {e}")
