"""
Radarr handlers: stuck imports, failed downloads.
"""

import re
import time

from watchdog import config
from watchdog.api import radarr as api
from watchdog.plex import refresh_path as plex_refresh_path

log = config.logger

# Releases we've removed as "failed" — tracked so we can clear any
# blocklist entries Radarr's native failure handler adds for the same
# releases. Mirrors the same mechanism in watchdog.sonarr.
_recently_cleared_failures: list = []
FAILURE_CLEANUP_WINDOW = 10 * 60

_WS_RE = re.compile(r"\s+")


def _normalize_release_title(title):
    return _WS_RE.sub(" ", (title or "").strip().lower())


def handle_stuck_imports():
    """Auto-import stuck Radarr queue items with warnings."""
    if not config.radarr_api_key:
        return
    try:
        queue = api("GET", "/api/v3/queue?pageSize=200&includeUnknownMovieItems=true")
    except Exception:
        return

    for item in (queue or {}).get("records", []):
        if item.get("trackedDownloadStatus") != "warning" or item.get("trackedDownloadState") != "importing":
            continue

        title = item.get("title", "?")[:60]
        movie_id = item.get("movieId")
        download_id = item.get("downloadId")
        if not movie_id or not download_id:
            continue

        log.info(f"Radarr stuck import: {title}")

        try:
            scan = api("GET",
                f"/api/v3/manualimport?downloadId={download_id}"
                f"&movieId={movie_id}&filterExistingFiles=false",
                timeout=60)

            files = []
            for f in (scan or []):
                if not f.get("movie"):
                    continue
                rejections = f.get("rejections", [])
                has_permanent_block = any(
                    r.get("type") == "permanent" and "sample" not in r.get("reason", "").lower()
                    for r in rejections
                )
                if has_permanent_block:
                    continue
                files.append({
                    "path": f["path"],
                    "movieId": f["movie"]["id"],
                    "quality": f["quality"],
                    "languages": f.get("languages", [{"id": 1, "name": "English"}]),
                    "indexerFlags": 0,
                    "downloadId": download_id,
                })

            if files:
                result = api("POST", "/api/v3/command", {
                    "name": "ManualImport",
                    "files": files,
                })
                log.info(f"  Radarr auto-imported {len(files)} file(s): {(result or {}).get('status', '?')}")
                try:
                    movie_info = api("GET", f"/api/v3/movie/{movie_id}")
                    if movie_info and movie_info.get("path"):
                        plex_refresh_path(movie_info["path"])
                except Exception as e:
                    log.debug(f"  Plex scan trigger failed: {e}")
            else:
                log.warning(f"  No importable files for: {title}")
        except Exception as e:
            log.error(f"  Radarr import error for {title}: {e}")


def handle_failed_downloads():
    """
    Remove failed Radarr downloads without blocklisting and re-search.
    Also clears any blocklist entries Radarr added via its own failure
    handler for these releases — otherwise the re-search skips good
    releases and the movie stays stuck.
    """
    global _recently_cleared_failures

    if not config.radarr_api_key:
        return
    try:
        queue = api("GET", "/api/v3/queue?pageSize=200&includeUnknownMovieItems=true")
    except Exception:
        return

    movie_ids_to_search = []

    for item in (queue or {}).get("records", []):
        state = item.get("trackedDownloadState", "")
        tracked_status = item.get("trackedDownloadStatus", "")
        status = item.get("status", "")

        is_failed = (
            state == "importFailed"
            or tracked_status == "error"
            or status == "failed"
        )

        if not is_failed:
            continue

        item_title = item.get("title", "")
        title = item_title[:60] or "?"
        item_id = item["id"]
        movie_id = item.get("movieId")
        log.info(f"Radarr failed download: {title}")

        try:
            api("DELETE",
                f"/api/v3/queue/{item_id}?removeFromClient=true&blocklist=false&skipRedownload=true")
            log.info(f"  Removed from queue")
            if movie_id:
                movie_ids_to_search.append(movie_id)
            _recently_cleared_failures.append({
                "title_norm": _normalize_release_title(item_title),
                "movie_id": movie_id,
                "timestamp": time.time(),
            })
        except Exception as e:
            log.error(f"  Error: {e}")

    now = time.time()
    _recently_cleared_failures = [
        f for f in _recently_cleared_failures
        if now - f["timestamp"] < FAILURE_CLEANUP_WINDOW
    ]
    if _recently_cleared_failures:
        _clear_auto_blocklist_for_failures()

    for mid in movie_ids_to_search:
        try:
            api("POST", "/api/v3/command", {
                "name": "MoviesSearch",
                "movieIds": [mid],
            })
            log.info(f"  Re-searching movie {mid}")
            time.sleep(config.EPISODE_SEARCH_DELAY)
        except Exception as e:
            log.error(f"  Movie search error for {mid}: {e}")


def _clear_auto_blocklist_for_failures():
    """
    Fetch the Radarr blocklist and delete any entries whose sourceTitle
    matches a release we recently chose to retry. Closes the race where
    Radarr auto-blocklists a release either just before or just after
    our queue-removal call.
    """
    try:
        blocklist = api("GET", "/api/v3/blocklist?page=1&pageSize=500")
    except Exception as e:
        log.debug(f"  Could not fetch Radarr blocklist for auto-cleanup: {e}")
        return

    records = (blocklist or {}).get("records", [])
    if not records:
        return

    failure_titles = {f["title_norm"] for f in _recently_cleared_failures if f["title_norm"]}
    if not failure_titles:
        return

    cleared = 0
    for record in records:
        source_title_norm = _normalize_release_title(record.get("sourceTitle", ""))
        if source_title_norm and source_title_norm in failure_titles:
            try:
                api("DELETE", f"/api/v3/blocklist/{record['id']}")
                cleared += 1
            except Exception as e:
                log.debug(f"  Failed to clear Radarr auto-blocklist entry {record['id']}: {e}")

    if cleared:
        log.info(f"  Cleared {cleared} Radarr auto-blocklist entr{'y' if cleared == 1 else 'ies'} for recently-failed releases")
