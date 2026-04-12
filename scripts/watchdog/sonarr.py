"""
Sonarr handlers: watchlist sync, new series detection, anime rerouting,
stuck imports, failed downloads, blocklist hygiene, missing episode sweep,
and symlink reconciliation for anime imports that Sonarr can't auto-import.
"""

import os
import re
import subprocess
import time
import urllib.error
from datetime import datetime, timezone

from watchdog import config
from watchdog.api import sonarr as api, radarr as radarr_api, plex_discover as plex_discover_api
from watchdog.plex import refresh_path as plex_refresh_path

log = config.logger

# ─── Module state ─────────────────────────────────────────────────────

known_series_ids: set = set()
initial_snapshot_taken = False
last_missing_sweep = 0.0
last_blocklist_hygiene = 0.0
last_symlink_reconcile = 0.0
last_symlink_cleanup = 0.0

_stuck_since: dict = {}

# Releases the watchdog has removed from the queue as "failed" — tracked
# so we can clean up any blocklist entries Sonarr's own failure handler
# adds for these same releases (which can happen before OR after we remove
# the queue item, so we need a rolling window).
_recently_cleared_failures: list = []
FAILURE_CLEANUP_WINDOW = 10 * 60

_WS_RE = re.compile(r"\s+")


def _normalize_release_title(title):
    return _WS_RE.sub(" ", (title or "").strip().lower())


# ─── Watchlist sync ───────────────────────────────────────────────────

def sync_watchlist():
    """Trigger ImportListSync on both Sonarr and Radarr, then periodically
    diff the Plex watchlist to unmonitor items that were removed."""
    try:
        api("POST", "/api/v3/command", {"name": "ImportListSync"})
    except urllib.error.HTTPError as e:
        if e.code == 500:
            config.force_health_check = True
        log.warning(f"Sonarr ImportListSync failed: {e}")
    except Exception as e:
        log.warning(f"Sonarr ImportListSync failed: {e}")

    try:
        radarr_api("POST", "/api/v3/command", {"name": "ImportListSync"})
    except urllib.error.HTTPError as e:
        if e.code == 500:
            config.force_health_check = True
        log.warning(f"Radarr ImportListSync failed: {e}")
    except Exception as e:
        log.warning(f"Radarr ImportListSync failed: {e}")

    _diff_watchlist()


def _diff_watchlist():
    """Compare Plex watchlist against Sonarr/Radarr and unmonitor removed items."""
    try:
        data = plex_discover_api("/library/sections/watchlist/all")
    except Exception as e:
        log.warning(f"Watchlist diff: failed to fetch Plex watchlist: {e}")
        return

    if not data:
        return

    items = (data.get("MediaContainer") or {}).get("Metadata") or []
    if not items:
        log.info("Watchlist diff: Plex watchlist is empty — skipping to avoid accidental unmonitor")
        return

    watchlist_tvdb: set = set()
    watchlist_tmdb: set = set()

    for item in items:
        for guid in item.get("Guid") or []:
            gid = guid.get("id", "")
            if gid.startswith("tvdb://"):
                try:
                    watchlist_tvdb.add(int(gid[7:]))
                except ValueError:
                    pass
            elif gid.startswith("tmdb://"):
                try:
                    watchlist_tmdb.add(int(gid[7:]))
                except ValueError:
                    pass

    if not watchlist_tvdb and not watchlist_tmdb:
        log.info("Watchlist diff: no parseable GUIDs — skipping")
        return

    if watchlist_tvdb:
        try:
            all_series = api("GET", "/api/v3/series") or []
            for s in all_series:
                tvdb_id = s.get("tvdbId")
                if tvdb_id and tvdb_id not in watchlist_tvdb:
                    log.info(f"Watchlist diff: deleting series '{s['title']}' (tvdb:{tvdb_id}) — removed from watchlist")
                    try:
                        api("DELETE", f"/api/v3/series/{s['id']}?deleteFiles=true")
                    except Exception as e:
                        log.error(f"  Failed to delete series {s['id']}: {e}")
        except Exception as e:
            log.warning(f"Watchlist diff: Sonarr series check failed: {e}")

    if watchlist_tmdb and config.radarr_api_key:
        try:
            all_movies = radarr_api("GET", "/api/v3/movie") or []
            for m in all_movies:
                tmdb_id = m.get("tmdbId")
                if tmdb_id and tmdb_id not in watchlist_tmdb:
                    log.info(f"Watchlist diff: deleting movie '{m['title']}' (tmdb:{tmdb_id}) — removed from watchlist")
                    try:
                        radarr_api("DELETE", f"/api/v3/movie/{m['id']}?deleteFiles=true")
                    except Exception as e:
                        log.error(f"  Failed to delete movie {m['id']}: {e}")
        except Exception as e:
            log.warning(f"Watchlist diff: Radarr movie check failed: {e}")


# ─── Series snapshot + new series detection ───────────────────────────

def get_all_series():
    result = api("GET", "/api/v3/series")
    return {s["id"]: s for s in (result or [])}


def snapshot_series():
    global known_series_ids, initial_snapshot_taken
    series = get_all_series()
    known_series_ids = set(series.keys())
    initial_snapshot_taken = True
    log.info(f"Snapshot: {len(known_series_ids)} existing series")


def is_anime(series):
    return "Anime" in series.get("genres", [])


def reroute_anime(series):
    """If a series is anime but was added with TV defaults, fix its config."""
    sid = series["id"]
    title = series["title"]

    if not is_anime(series):
        return

    needs_update = (
        series["rootFolderPath"] != config.ANIME_ROOT
        or series["seriesType"] != "anime"
        or series["qualityProfileId"] != config.ANIME_QUALITY_PROFILE_ID
    )

    if not needs_update:
        return

    log.info(f"Rerouting anime: {title} -> {config.ANIME_ROOT}")

    series["rootFolderPath"] = config.ANIME_ROOT
    series["seriesType"] = "anime"
    series["qualityProfileId"] = config.ANIME_QUALITY_PROFILE_ID
    old_path = series["path"]
    series_folder = old_path.rsplit("/", 1)[-1]
    series["path"] = f"{config.ANIME_ROOT}/{series_folder}"

    try:
        api("PUT", f"/api/v3/series/{sid}?moveFiles=true", series)
        log.info(f"  Updated and moved {title}")
    except Exception as e:
        log.error(f"  Reroute error for {title}: {e}")


def detect_and_search_new_series():
    """
    Compare current series against our snapshot. For any new series:
    1. Reroute anime to the anime folder/profile/type
    2. Search episodes one-by-one starting from S01E01
    """
    global known_series_ids

    current = get_all_series()
    current_ids = set(current.keys())
    new_ids = current_ids - known_series_ids

    if not new_ids:
        known_series_ids = current_ids
        return

    for sid in new_ids:
        series = current[sid]
        title = series["title"]
        log.info(f"New series detected: {title} (ID: {sid})")

        reroute_anime(series)

        episodes = api("GET", f"/api/v3/episode?seriesId={sid}")
        missing = [
            ep for ep in episodes
            if ep.get("monitored") and not ep.get("hasFile") and ep["seasonNumber"] > 0
        ]
        missing.sort(key=lambda e: (e["seasonNumber"], e["episodeNumber"]))

        if not missing:
            log.info(f"  No missing episodes for {title}")
            continue

        log.info(f"  Searching {len(missing)} episodes one-by-one for {title}")
        for i, ep in enumerate(missing):
            label = f"S{ep['seasonNumber']:02d}E{ep['episodeNumber']:02d}"
            try:
                api("POST", "/api/v3/command", {
                    "name": "EpisodeSearch",
                    "episodeIds": [ep["id"]],
                })
                log.info(f"  [{i+1}/{len(missing)}] Searched {label}")
            except Exception as e:
                log.error(f"  Search error for {label}: {e}")

            if i < len(missing) - 1:
                time.sleep(config.EPISODE_SEARCH_DELAY)

    known_series_ids = current_ids


# ─── Stuck imports ────────────────────────────────────────────────────

def handle_stuck_imports():
    """
    Handle ALL queue items stuck with warnings, including:
    - "matched to series by ID" (name mismatch)
    - "Unable to determine if file is a sample" (obfuscated filenames)
    - Any other importable warning state

    Items stuck longer than DEAD_DOWNLOAD_GRACE are probed for I/O errors
    (DMCA'd Usenet articles). Dead downloads are blocklisted and re-searched.
    """
    global _stuck_since

    queue = api("GET", "/api/v3/queue?pageSize=200&includeUnknownSeriesItems=true")
    active_ids = set()

    for item in queue.get("records", []):
        tracked_status = item.get("trackedDownloadStatus", "")
        tracked_state = item.get("trackedDownloadState", "")

        importable_states = {"importing", "importBlocked", "importPending"}
        if tracked_status != "warning" or tracked_state not in importable_states:
            continue

        title = item.get("title", "?")[:60]
        series_id = item.get("seriesId")
        download_id = item.get("downloadId")
        if not series_id or not download_id:
            continue

        active_ids.add(download_id)
        now = time.time()
        first_seen = _stuck_since.setdefault(download_id, now)

        if now - first_seen >= config.DEAD_DOWNLOAD_GRACE:
            if _probe_dead_download(item):
                _stuck_since.pop(download_id, None)
                continue

        warning_reasons = []
        for msg in item.get("statusMessages", []):
            for m in msg.get("messages", []):
                warning_reasons.append(m)

        log.info(f"Stuck import detected: {title} — {'; '.join(warning_reasons)}")

        try:
            scan = api("GET",
                f"/api/v3/manualimport?downloadId={download_id}"
                f"&seriesId={series_id}&filterExistingFiles=false",
                timeout=60)

            files = []
            for f in scan:
                if not f.get("series") or not f.get("episodes"):
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
                    "seriesId": f["series"]["id"],
                    "episodeIds": [e["id"] for e in f["episodes"]],
                    "quality": f["quality"],
                    "languages": f.get("languages", [{"id": 1, "name": "English"}]),
                    "indexerFlags": 0,
                    "releaseType": "singleEpisode",
                    "downloadId": download_id,
                })

            if files:
                result = api("POST", "/api/v3/command", {
                    "name": "ManualImport",
                    "files": files,
                })
                log.info(f"  Auto-imported {len(files)} file(s): {result.get('status', '?')}")
                _stuck_since.pop(download_id, None)
                try:
                    series_info = api("GET", f"/api/v3/series/{series_id}")
                    if series_info and series_info.get("path"):
                        plex_refresh_path(series_info["path"])
                except Exception as e:
                    log.debug(f"  Plex scan trigger failed: {e}")
            else:
                log.warning(f"  No importable files for: {title}")

        except Exception as e:
            log.error(f"  Import error for {title}: {e}")

    _stuck_since = {k: v for k, v in _stuck_since.items() if k in active_ids}


def _probe_dead_download(item):
    """Probe whether a stuck download's files are actually readable.
    Returns True if the download was dead and has been handled."""
    download_id = item.get("downloadId")
    title = item.get("title", "?")[:60]
    item_id = item["id"]
    episode_id = item.get("episodeId")
    output_path = item.get("outputPath", "")

    if not output_path:
        return False

    probe = subprocess.run(
        ["docker", "exec", "sonarr", "head", "-c", "1", output_path],
        capture_output=True, text=True, timeout=15,
    )

    if probe.returncode == 0:
        return False

    stderr = probe.stderr.lower()
    if "i/o error" not in stderr and "no such file" not in stderr and "input/output error" not in stderr:
        return False

    log.info(f"Dead download detected (I/O error): {title} — blocklisting and re-searching")

    try:
        api("DELETE",
            f"/api/v3/queue/{item_id}?removeFromClient=true&blocklist=true&skipRedownload=true")
    except Exception as e:
        log.error(f"  Failed to remove dead download {title}: {e}")
        return False

    if episode_id:
        try:
            time.sleep(config.EPISODE_SEARCH_DELAY)
            api("POST", "/api/v3/command", {
                "name": "EpisodeSearch",
                "episodeIds": [episode_id],
            })
            log.info(f"  Re-searching episode {episode_id}")
        except Exception as e:
            log.error(f"  Re-search failed for episode {episode_id}: {e}")

    return True


# ─── Failed downloads ─────────────────────────────────────────────────

def handle_failed_downloads():
    """
    Catch all failure states and remove without blocklisting so a different
    release can be tried. Re-searches affected episodes individually.

    Also clears any blocklist entries Sonarr's own failure handler added
    for these releases — otherwise the re-search would skip perfectly
    good releases, and the blocklist-death-spiral begins.
    """
    global _recently_cleared_failures

    queue = api("GET", "/api/v3/queue?pageSize=200&includeUnknownSeriesItems=true")
    episode_ids_to_search = []

    for item in queue.get("records", []):
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
        episode_id = item.get("episodeId")
        log.info(f"Failed download detected: {title} (status={status} state={state} tracked={tracked_status})")

        try:
            api("DELETE",
                f"/api/v3/queue/{item_id}?removeFromClient=true&blocklist=false&skipRedownload=true")
            log.info(f"  Removed failed item from queue")
            if episode_id:
                episode_ids_to_search.append(episode_id)
            _recently_cleared_failures.append({
                "title_norm": _normalize_release_title(item_title),
                "episode_id": episode_id,
                "timestamp": time.time(),
            })
        except Exception as e:
            log.error(f"  Retry error for {title}: {e}")

    # Prune the rolling window and scan blocklist for auto-entries we need
    # to clear. We do this every cycle (even if nothing new failed) because
    # Sonarr may auto-blocklist a release AFTER we remove its queue item.
    now = time.time()
    _recently_cleared_failures = [
        f for f in _recently_cleared_failures
        if now - f["timestamp"] < FAILURE_CLEANUP_WINDOW
    ]
    if _recently_cleared_failures:
        _clear_auto_blocklist_for_failures()

    for eid in episode_ids_to_search:
        try:
            api("POST", "/api/v3/command", {
                "name": "EpisodeSearch",
                "episodeIds": [eid],
            })
            log.info(f"  Re-searching episode {eid}")
            time.sleep(config.EPISODE_SEARCH_DELAY)
        except Exception as e:
            log.error(f"  Episode search error for {eid}: {e}")


def _clear_auto_blocklist_for_failures():
    """
    Fetch the blocklist and delete any entries whose sourceTitle matches
    a release we've recently chosen to retry. This closes the race where
    Sonarr's native failure handler blocklists a release either just
    before or just after our queue-removal call.
    """
    try:
        blocklist = api("GET", "/api/v3/blocklist?page=1&pageSize=500")
    except Exception as e:
        log.debug(f"  Could not fetch blocklist for auto-cleanup: {e}")
        return

    records = blocklist.get("records", [])
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
                log.debug(f"  Failed to clear auto-blocklist entry {record['id']}: {e}")

    if cleared:
        log.info(f"  Cleared {cleared} auto-blocklist entr{'y' if cleared == 1 else 'ies'} for recently-failed releases")


# ─── Blocklist hygiene ────────────────────────────────────────────────

def blocklist_hygiene():
    """
    Prevent the blocklist death spiral: when all releases for an episode are
    blocklisted, it can never be found again. Periodically clear blocklist
    entries for episodes that are still missing, then re-search them.
    """
    global last_blocklist_hygiene
    now = time.time()
    if now - last_blocklist_hygiene < config.BLOCKLIST_HYGIENE_INTERVAL:
        return
    last_blocklist_hygiene = now

    log.info("Blocklist hygiene: scanning for stuck episodes")

    missing_episode_ids = set()
    series_list = api("GET", "/api/v3/series")
    for s in series_list:
        stats = s.get("statistics", {})
        if stats.get("episodeFileCount", 0) >= stats.get("episodeCount", 0):
            continue

        episodes = api("GET", f"/api/v3/episode?seriesId={s['id']}")
        utcnow = datetime.now(timezone.utc).isoformat()
        for ep in episodes:
            if ep["seasonNumber"] == 0 or not ep.get("monitored") or ep.get("hasFile"):
                continue
            airdate = ep.get("airDateUtc", "")
            if airdate and airdate < utcnow:
                missing_episode_ids.add(ep["id"])

    if not missing_episode_ids:
        log.info("  No missing aired episodes — blocklist clean")
        return

    blocklist = api("GET", "/api/v3/blocklist?page=1&pageSize=1000")
    records = blocklist.get("records", [])

    cleared = 0
    episode_ids_to_search = set()
    for record in records:
        ep_ids = [e.get("id") for e in record.get("episodes", [])]
        if any(eid in missing_episode_ids for eid in ep_ids):
            try:
                api("DELETE", f"/api/v3/blocklist/{record['id']}")
                cleared += 1
                episode_ids_to_search.update(eid for eid in ep_ids if eid in missing_episode_ids)
            except Exception as e:
                log.error(f"  Failed to clear blocklist entry {record['id']}: {e}")

    log.info(f"  Cleared {cleared} blocklist entries for {len(episode_ids_to_search)} missing episodes")

    for eid in episode_ids_to_search:
        try:
            api("POST", "/api/v3/command", {"name": "EpisodeSearch", "episodeIds": [eid]})
            time.sleep(config.EPISODE_SEARCH_DELAY)
        except Exception as e:
            log.error(f"  Re-search failed for episode {eid}: {e}")


# ─── Missing episode sweep ────────────────────────────────────────────

def sweep_missing_episodes():
    """
    Every MISSING_SWEEP_INTERVAL, find all missing aired episodes across all
    series and trigger individual episode searches.
    """
    global last_missing_sweep
    now = time.time()
    if now - last_missing_sweep < config.MISSING_SWEEP_INTERVAL:
        return
    last_missing_sweep = now

    log.info("Missing episode sweep: scanning all series")

    total_missing = 0
    total_searched = 0

    series_list = api("GET", "/api/v3/series")
    for s in series_list:
        stats = s.get("statistics", {})
        if stats.get("episodeFileCount", 0) >= stats.get("episodeCount", 0):
            continue

        episodes = api("GET", f"/api/v3/episode?seriesId={s['id']}")
        utcnow = datetime.now(timezone.utc).isoformat()
        missing = [
            ep for ep in episodes
            if ep["seasonNumber"] > 0
            and ep.get("monitored")
            and not ep.get("hasFile")
            and ep.get("airDateUtc", "") < utcnow
            and ep.get("airDateUtc", "") != ""
        ]

        if not missing:
            continue

        total_missing += len(missing)
        title = s["title"]
        log.info(f"  {title}: {len(missing)} missing aired episodes — searching")

        for ep in sorted(missing, key=lambda e: (e["seasonNumber"], e["episodeNumber"])):
            label = f"S{ep['seasonNumber']:02d}E{ep['episodeNumber']:02d}"
            try:
                api("POST", "/api/v3/command", {"name": "EpisodeSearch", "episodeIds": [ep["id"]]})
                total_searched += 1
                log.info(f"    Searched {label}")
                time.sleep(config.EPISODE_SEARCH_DELAY)
            except Exception as e:
                log.error(f"    Search failed for {label}: {e}")

    log.info(f"  Sweep complete: {total_searched}/{total_missing} episodes searched")


# ─── Symlink reconciliation for anime ────────────────────────────────

_ABS_PATTERNS = [
    re.compile(r'[Gg]intama\s*\(\d{4}\)\s*-\s*\d+\s*\((\d+)\)'),
    re.compile(r'[Gg]intama\s*-\s*\d+\s*\((\d+)\)'),
    re.compile(r'[Gg]intama\s*-\s*(\d+)\s*[\[\(]'),
    re.compile(r'GINTAMA\s*-\s*(\d+)\s'),
    re.compile(r'[Gg]intama\s*\(\d{4}\)\s*-\s*(\d+)'),
    re.compile(r'[Gg]intama\S*\s*-\s*(\d+)'),
    re.compile(r'[Gg]intama[.-](\d+)[.\s]'),
    re.compile(r'[Gg]intama\s+(\d+)\s'),
    re.compile(r'[Gg]intama-(\d+)\.'),
]


def _extract_abs_number(filename):
    """Extract absolute episode number from an anime release filename."""
    for pattern in _ABS_PATTERNS:
        m = pattern.search(filename)
        if m:
            return int(m.group(1))
    return None


def _extract_quality(path):
    low = path.lower()
    if '1080p' in low or '1920x1080' in low:
        return '1080p'
    if '720p' in low or '1280x720' in low:
        return '720p'
    return '720p'


def _quality_score(path):
    low = path.lower()
    score = 0
    if '1080p' in low or '1920x1080' in low:
        score += 100
    elif '720p' in low or '1280x720' in low:
        score += 50
    if 'bluray' in low or ' bd ' in low or '.bd.' in low:
        score += 20
    if 'flac' in low:
        score += 10
    if re.search(r'\(\d+\)/', path):
        score -= 200
    return score


def reconcile_anime_symlinks():
    """
    For anime series with missing episodes, check if NzbDAV already has the
    files in completed-symlinks but Sonarr failed to auto-import them (e.g.
    due to "matched by ID" blocking). Creates standardized symlinks directly
    in the media library and triggers a refresh.

    Runs every SYMLINK_RECONCILE_INTERVAL (default 4 hours).
    """
    global last_symlink_reconcile
    now = time.time()
    if now - last_symlink_reconcile < config.SYMLINK_RECONCILE_INTERVAL:
        return
    last_symlink_reconcile = now

    log.info("Symlink reconciliation: checking anime series")

    series_list = api("GET", "/api/v3/series")
    anime_series = [
        s for s in series_list
        if s.get("seriesType") == "anime"
        and s.get("statistics", {}).get("episodeFileCount", 0)
           < s.get("statistics", {}).get("episodeCount", 0)
    ]

    if not anime_series:
        log.info("  All anime series complete — nothing to reconcile")
        return

    nzbdav_files = _list_nzbdav_anime_files()
    if not nzbdav_files:
        log.info("  No anime files found in NzbDAV completed-symlinks")
        return

    refreshed_series = set()

    for series in anime_series:
        sid = series["id"]
        title = series["title"]
        series_path = series["path"]

        episodes = api("GET", f"/api/v3/episode?seriesId={sid}")
        missing = {
            ep["absoluteEpisodeNumber"]: (ep["seasonNumber"], ep["episodeNumber"])
            for ep in episodes
            if ep.get("absoluteEpisodeNumber")
            and not ep.get("hasFile")
            and ep.get("monitored")
            and ep["seasonNumber"] > 0
        }

        if not missing:
            continue

        title_lower = title.lower().replace("'", "").replace("°", "")
        matching_files = [
            f for f in nzbdav_files
            if title_lower in os.path.basename(f).lower().replace("'", "").replace("°", "")
        ]

        if not matching_files:
            continue

        abs_to_files = {}
        for fpath in matching_files:
            abs_num = _extract_abs_number(os.path.basename(fpath))
            if abs_num and abs_num in missing:
                abs_to_files.setdefault(abs_num, []).append(fpath)

        if not abs_to_files:
            continue

        created = 0
        for abs_num, candidates in abs_to_files.items():
            season, ep = missing[abs_num]
            best = max(candidates, key=_quality_score)
            quality = _extract_quality(best)

            season_dir = f"{series_path}/Season {season}"
            symlink_name = f"{title} - {abs_num:03d} [{quality}].mkv"
            dest = f"{season_dir}/{symlink_name}"

            result = subprocess.run(
                ["docker", "exec", "sonarr", "sh", "-c",
                 f'mkdir -p "{season_dir}" && '
                 f'[ ! -e "{dest}" ] && ln -sf "{best}" "{dest}"'],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode == 0:
                created += 1

        if created:
            log.info(f"  {title}: created {created} symlinks for missing episodes")
            refreshed_series.add(sid)

    series_path_by_id = {s["id"]: s.get("path") for s in anime_series}
    for sid in refreshed_series:
        try:
            api("POST", "/api/v3/command", {"name": "RefreshSeries", "seriesId": sid})
        except Exception as e:
            log.error(f"  RefreshSeries failed for {sid}: {e}")
        path = series_path_by_id.get(sid)
        if path:
            try:
                plex_refresh_path(path)
            except Exception as e:
                log.debug(f"  Plex scan trigger failed for {sid}: {e}")

    log.info(f"  Reconciliation complete: refreshed {len(refreshed_series)} series")


def _list_nzbdav_anime_files():
    """List .mkv files in NzbDAV's completed-symlinks/tv/ directory."""
    try:
        result = subprocess.run(
            ["docker", "exec", "nzbdav_rclone", "find",
             "/mnt/remote/nzbdav/completed-symlinks/tv/",
             "-maxdepth", "2", "-name", "*.mkv"],
            capture_output=True, text=True, timeout=60,
        )
        if result.returncode != 0:
            return []
        return [line.strip() for line in result.stdout.strip().split("\n") if line.strip()]
    except Exception as e:
        log.error(f"  Failed to list NzbDAV files: {e}")
        return []


# ─── Stale symlink cleanup ───────────────────────────────────────────

_cleanup_proc = None


def cleanup_stale_symlinks():
    """
    Periodically remove duplicate (N) folders from completed-symlinks that
    accumulate when Sonarr retries imports. Runs non-blocking via Popen
    since deletion over FUSE can take minutes.
    """
    global last_symlink_cleanup, _cleanup_proc

    if _cleanup_proc is not None:
        exit_code = _cleanup_proc.poll()
        if exit_code is None:
            return
        if exit_code == 0:
            log.info("Stale symlink cleanup: background job finished")
        else:
            log.warning(f"Stale symlink cleanup: background job exited with code {exit_code}")
        _cleanup_proc = None

    now = time.time()
    if now - last_symlink_cleanup < config.SYMLINK_CLEANUP_INTERVAL:
        return
    last_symlink_cleanup = now

    log.info("Stale symlink cleanup: removing duplicate (N) folders from completed-symlinks")
    try:
        _cleanup_proc = subprocess.Popen(
            ["docker", "exec", "sonarr", "find",
             "/mnt/remote/nzbdav/completed-symlinks/tv/",
             "-maxdepth", "1", "-type", "d", "-regex", r".* ([0-9]+)",
             "-exec", "rm", "-rf", "{}", "+"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except Exception as e:
        log.error(f"Stale symlink cleanup: failed to start: {e}")
