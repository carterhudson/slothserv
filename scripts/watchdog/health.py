"""
Health checks: database integrity auto-repair and import list staleness detection.
"""

import json
import re
import shutil
import sqlite3 as _sqlite3
import subprocess
import time
import urllib.error
import urllib.request

from pathlib import Path

from watchdog import config
from watchdog.api import sonarr as sonarr_api, radarr as radarr_api

log = config.logger

# ─── Module state ─────────────────────────────────────────────────────

last_health_check = 0.0

# Track when a normalized title was first seen as "missing from library"
# so we don't thrash the import list on items that Sonarr just hasn't
# ingested yet.
_missing_since: dict = {"sonarr": {}, "radarr": {}}

# Only recreate an import list if a watchlist item has been missing this
# long. Gives ImportListSync time to pick it up on its own.
IMPORT_LIST_RECREATE_DELAY = 4 * 3600

_NORM_RE = re.compile(r"[^a-z0-9]+")


def _normalize_title(title):
    return _NORM_RE.sub("", (title or "").lower())


# ─── Public entry point ──────────────────────────────────────────────

def health_check():
    """
    Hourly health check (or on-demand after a 500 error) that catches:
    1. Database corruption — detected by attempting Sonarr/Radarr API commands
    2. Stale import lists — detected by comparing Plex Watchlist with *arr libraries
    """
    global last_health_check
    now = time.time()
    if not config.force_health_check and now - last_health_check < config.HEALTH_CHECK_INTERVAL:
        return
    last_health_check = now
    config.force_health_check = False

    log.info("Health check: testing DB integrity and import list health")

    _test_and_repair_db("sonarr")
    if config.radarr_api_key:
        _test_and_repair_db("radarr")

    _verify_import_lists()


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
    Compare Plex Watchlist with Sonarr/Radarr libraries. For items that are
    on the watchlist but not yet in the library, first trigger an
    ImportListSync. Only if the same items remain missing past
    IMPORT_LIST_RECREATE_DELAY do we delete and recreate the import list.
    """
    if not config.plex_token:
        return

    try:
        req = urllib.request.Request(
            "https://discover.provider.plex.tv/library/sections/watchlist/all"
            f"?X-Plex-Token={config.plex_token}",
            headers={"Accept": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read())
        watchlist = data.get("MediaContainer", {}).get("Metadata", [])
    except Exception as e:
        log.debug(f"  Could not fetch Plex Watchlist: {e}")
        return

    if not watchlist:
        log.info("  Plex Watchlist empty — nothing to verify")
        _missing_since["sonarr"].clear()
        _missing_since["radarr"].clear()
        return

    watchlist_shows = {
        _normalize_title(item["title"]): item["title"]
        for item in watchlist if item.get("type") == "show"
    }
    watchlist_movies = {
        _normalize_title(item["title"]): item["title"]
        for item in watchlist if item.get("type") == "movie"
    }

    if watchlist_shows:
        _verify_service("sonarr", watchlist_shows)
    if watchlist_movies and config.radarr_api_key:
        _verify_service("radarr", watchlist_movies)


def _verify_service(service, watchlist_titles):
    """
    watchlist_titles: {normalized_title: original_title}
    """
    call = sonarr_api if service == "sonarr" else radarr_api
    endpoint = "/api/v3/series" if service == "sonarr" else "/api/v3/movie"

    try:
        items = call("GET", endpoint) or []
    except Exception as e:
        log.error(f"  {service} library fetch failed: {e}")
        return

    library_titles = set()
    for it in items:
        library_titles.add(_normalize_title(it["title"]))
        for alt in it.get("alternateTitles", []) or []:
            library_titles.add(_normalize_title(alt["title"]))

    missing_norm = set(watchlist_titles.keys()) - library_titles
    now = time.time()
    tracked = _missing_since[service]

    # Clear tracking for items no longer missing.
    for n in list(tracked.keys()):
        if n not in missing_norm:
            del tracked[n]

    if not missing_norm:
        log.info(f"  {service} import list OK — all watchlist items present")
        return

    for n in missing_norm:
        tracked.setdefault(n, now)

    persistent = [n for n, t in tracked.items() if now - t >= IMPORT_LIST_RECREATE_DELAY]
    if persistent:
        persistent_titles = sorted(watchlist_titles[n] for n in persistent if n in watchlist_titles)
        hours = IMPORT_LIST_RECREATE_DELAY // 3600
        log.warning(
            f"  {service}: {len(persistent_titles)} watchlist item(s) missing >{hours}h: "
            f"{', '.join(persistent_titles[:5])}"
        )
        _recreate_import_list(service)
        tracked.clear()
    else:
        missing_titles = sorted(watchlist_titles[n] for n in missing_norm if n in watchlist_titles)
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
