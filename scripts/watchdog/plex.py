"""
Plex handlers: truncated episode detection and partial library scans.

Session monitoring is handled by Tautulli.
Subtitle management is handled by Bazarr.
"""

import time
import urllib.parse
import urllib.request

from watchdog import config
from watchdog.api import sonarr as sonarr_api, plex as plex_api

log = config.logger

# ─── Module state ─────────────────────────────────────────────────────

last_truncation_check = 0.0

# (section_id, [location_paths]) tuples, cached for SECTIONS_CACHE_TTL.
_sections_cache: list = []
_sections_cache_ts = 0.0
SECTIONS_CACHE_TTL = 3600


# ─── Partial library scans ───────────────────────────────────────────

def _get_sections():
    """Return cached Plex sections as [(section_id, [locations]), ...]."""
    global _sections_cache, _sections_cache_ts
    now = time.time()
    if _sections_cache and now - _sections_cache_ts < SECTIONS_CACHE_TTL:
        return _sections_cache
    try:
        data = plex_api("/library/sections")
        directories = data.get("MediaContainer", {}).get("Directory", [])
        cache = []
        for d in directories:
            locations = [loc["path"] for loc in d.get("Location", [])]
            cache.append((d["key"], locations))
        _sections_cache = cache
        _sections_cache_ts = now
    except Exception as e:
        log.debug(f"  Could not fetch Plex sections: {e}")
    return _sections_cache


def refresh_path(path):
    """
    Trigger a partial Plex library scan for a specific directory.
    Plex will only scan that path, not the whole library.
    Returns True if a scan was triggered.
    """
    if not path or not config.plex_token:
        return False

    for section_id, locations in _get_sections():
        for loc in locations:
            if path == loc or path.startswith(loc.rstrip("/") + "/"):
                encoded = urllib.parse.quote(path, safe="")
                url = (
                    f"{config.plex_url}/library/sections/{section_id}/refresh"
                    f"?path={encoded}&X-Plex-Token={config.plex_token}"
                )
                try:
                    with urllib.request.urlopen(url, timeout=10) as r:
                        r.read()
                    log.info(f"  Plex partial scan: {path}")
                    return True
                except Exception as e:
                    log.debug(f"  Plex refresh failed for {path}: {e}")
                    return False
    log.debug(f"  No Plex section matches path: {path}")
    return False


# ─── Truncated episode detection ─────────────────────────────────────

def detect_truncated_episodes():
    """
    Scan all Plex show libraries for episodes whose duration is under
    TRUNCATION_THRESHOLD of the show's median.  These are typically
    caused by incomplete Usenet downloads.

    Flagged files are deleted from Sonarr so the missing sweep re-searches.
    """
    global last_truncation_check
    now = time.time()
    if now - last_truncation_check < config.TRUNCATION_CHECK_INTERVAL:
        return
    last_truncation_check = now

    if not config.plex_token:
        return

    log.info("Truncation check: scanning Plex episodes")

    try:
        sections = plex_api("/library/sections")
        show_sections = [
            s for s in sections["MediaContainer"].get("Directory", [])
            if s["type"] == "show"
        ]
    except Exception as e:
        log.error(f"  Failed to get Plex libraries: {e}")
        return

    sonarr_series = sonarr_api("GET", "/api/v3/series") or []

    bad_file_ids = []

    for section in show_sections:
        try:
            shows = plex_api(f"/library/sections/{section['key']}/all")
            shows = shows["MediaContainer"].get("Metadata", [])
        except Exception:
            continue

        for show in shows:
            try:
                eps_data = plex_api(f"/library/metadata/{show['ratingKey']}/allLeaves")
                episodes = eps_data["MediaContainer"].get("Metadata", [])
            except Exception:
                continue

            if len(episodes) < 3:
                continue

            durations = [ep.get("duration", 0) / 60000 for ep in episodes]
            durations = [d for d in durations if d > 0]
            if not durations:
                continue

            sorted_durs = sorted(durations)
            mid = len(sorted_durs) // 2
            median_min = (sorted_durs[mid] if len(sorted_durs) % 2
                          else (sorted_durs[mid - 1] + sorted_durs[mid]) / 2)

            if median_min < 10:
                continue

            threshold = median_min * config.TRUNCATION_THRESHOLD
            show_title = show["title"]

            matched_series = None
            for s in sonarr_series:
                if s["title"].lower() == show_title.lower():
                    matched_series = s
                    break
                for alt in s.get("alternateTitles", []):
                    if alt["title"].lower() == show_title.lower():
                        matched_series = s
                        break
                if matched_series:
                    break

            if not matched_series:
                continue

            sonarr_eps = None

            for ep in episodes:
                ep_dur_min = ep.get("duration", 0) / 60000
                sn = ep.get("parentIndex", 0)
                en = ep.get("index", 0)

                if not (0 < ep_dur_min < threshold):
                    continue

                reason = (
                    f"Truncated: {show_title} S{sn:02d}E{en:02d} "
                    f"({ep_dur_min:.1f} min, median ~{median_min:.0f} min)"
                )

                if sonarr_eps is None:
                    try:
                        sonarr_eps = sonarr_api("GET", f"/api/v3/episode?seriesId={matched_series['id']}") or []
                    except Exception:
                        break

                for sep in sonarr_eps:
                    if sep["seasonNumber"] == sn and sep.get("episodeNumber") == en:
                        fid = sep.get("episodeFileId", 0)
                        if fid and fid not in bad_file_ids:
                            bad_file_ids.append(fid)
                            log.warning(f"  {reason} — deleting file {fid}")
                        break

    deleted = 0
    for fid in bad_file_ids:
        try:
            sonarr_api("DELETE", f"/api/v3/episodefile/{fid}")
            deleted += 1
        except Exception as e:
            log.error(f"  Failed to delete file {fid}: {e}")

    if deleted:
        log.info(f"  Deleted {deleted} bad file(s) — missing sweep will re-search them")
    else:
        log.info("  No truncated episodes found")
