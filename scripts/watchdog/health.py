"""
Health checks: container liveness, database integrity, and import list staleness.
"""

import shutil
import sqlite3 as _sqlite3
import subprocess
import time
import urllib.error
import urllib.request

from pathlib import Path

from watchdog import config
from watchdog.api import sonarr as sonarr_api, radarr as radarr_api, plex_discover as plex_discover_api

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

CRITICAL_CONTAINERS = ["sonarr", "gluetun", "nzbdav_rclone", "sabnzbd"]
OPTIONAL_CONTAINERS = ["radarr", "plex"]


# ─── Public entry point ──────────────────────────────────────────────

def health_check():
    """
    Hourly health check (or on-demand after a 500 error) that catches:
    1. Containers not running
    2. Database corruption
    3. Stale import lists (watchlist items not making it into the library)
    """
    global last_health_check
    now = time.time()
    if not config.force_health_check and now - last_health_check < config.HEALTH_CHECK_INTERVAL:
        return
    last_health_check = now
    config.force_health_check = False

    log.info("Health check: containers, DB integrity, import lists")

    _check_containers()

    _test_and_repair_db("sonarr")
    if config.radarr_api_key:
        _test_and_repair_db("radarr")

    _verify_import_lists()


# ─── Container liveness ─────────────────────────────────────────────

def _check_containers():
    """Verify critical containers are running; restart any that aren't."""
    all_containers = CRITICAL_CONTAINERS + (
        OPTIONAL_CONTAINERS if config.radarr_api_key else ["plex"]
    )

    for name in all_containers:
        try:
            result = subprocess.run(
                ["docker", "inspect", "--format", "{{.State.Status}}", name],
                capture_output=True, text=True, timeout=10, env=config.BREW_ENV,
            )
            status = result.stdout.strip()
        except Exception:
            status = "unknown"

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


# ─── DB integrity ────────────────────────────────────────────────────

def _test_and_repair_db(service):
    """Test both read and write API paths to detect DB corruption."""
    endpoints = [
        ("read", "/api/v3/series" if service == "sonarr" else "/api/v3/movie"),
        ("write", None),
    ]

    call = sonarr_api if service == "sonarr" else radarr_api

    for test_type, endpoint in endpoints:
        try:
            if test_type == "read":
                call("GET", endpoint)
            else:
                cmd = "RefreshSeries" if service == "sonarr" else "RefreshMovie"
                call("POST", "/api/v3/command", {"name": cmd})
        except urllib.error.HTTPError as e:
            if e.code == 500:
                body = ""
                try:
                    body = e.read().decode()
                except Exception:
                    pass
                if "database disk image is malformed" in body:
                    log.warning(f"  {service} DB corruption detected ({test_type} path) — starting auto-repair")
                    _repair_database(service)
                    return
                else:
                    log.error(f"  {service} API 500 on {test_type} path (not DB corruption): {body[:200]}")
        except Exception:
            pass


def _repair_database(service):
    """Stop container, rebuild DB via SQLite recover/dump, restart container."""
    db_path = config.BASE_DIR / f"config/{service}/{service}.db"
    if not db_path.exists():
        log.error(f"  {service} DB not found at {db_path}")
        return

    try:
        subprocess.run(
            ["docker", "compose", "stop", service],
            cwd=str(config.BASE_DIR), capture_output=True, timeout=30, env=config.BREW_ENV,
        )
        log.info(f"  Stopped {service} container")
    except Exception as e:
        log.error(f"  Failed to stop {service}: {e}")
        return

    bak = db_path.with_suffix(".db.bak")
    shutil.copy2(str(db_path), str(bak))

    try:
        conn = _sqlite3.connect(str(db_path))
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.close()
    except Exception:
        pass

    dump = subprocess.run(
        ["sqlite3", str(db_path), ".recover"],
        capture_output=True, text=True, timeout=120,
    )
    if dump.returncode != 0 or not dump.stdout.strip():
        log.warning(f"  .recover failed or empty, falling back to .dump")
        dump = subprocess.run(
            ["sqlite3", str(db_path), ".dump"],
            capture_output=True, text=True, timeout=60,
        )
    if dump.returncode != 0:
        log.error(f"  DB dump failed: {dump.stderr[:200]}")
        subprocess.run(
            ["docker", "compose", "start", service],
            cwd=str(config.BASE_DIR), capture_output=True, timeout=30, env=config.BREW_ENV,
        )
        return

    rebuilt = Path(f"/tmp/{service}_rebuilt.db")
    rebuild = subprocess.run(
        ["sqlite3", str(rebuilt)],
        input=dump.stdout, capture_output=True, text=True, timeout=60,
    )
    if rebuild.returncode != 0:
        log.error(f"  DB rebuild failed: {rebuild.stderr[:200]}")
        rebuilt.unlink(missing_ok=True)
        subprocess.run(
            ["docker", "compose", "start", service],
            cwd=str(config.BASE_DIR), capture_output=True, timeout=30, env=config.BREW_ENV,
        )
        return

    shutil.move(str(rebuilt), str(db_path))
    db_path.with_suffix(".db-wal").unlink(missing_ok=True)
    db_path.with_suffix(".db-shm").unlink(missing_ok=True)
    log.info(f"  {service} DB rebuilt successfully")

    subprocess.run(
        ["docker", "compose", "start", service],
        cwd=str(config.BASE_DIR), capture_output=True, timeout=30, env=config.BREW_ENV,
    )
    time.sleep(15)
    log.info(f"  {service} container restarted")


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
        data = plex_discover_api("/library/sections/watchlist/all")
    except Exception as e:
        log.debug(f"  Could not fetch Plex Watchlist: {e}")
        return

    if not data:
        return

    items = (data.get("MediaContainer") or {}).get("Metadata") or []
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
