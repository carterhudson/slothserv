"""
NzbDAV health monitoring: detect files with missing Usenet articles and
trigger re-downloads via Sonarr/Radarr.

NzbDAV performs its own periodic health checks, recording results in its
SQLite database.  This module reads those results and handles any unhealthy
files that NzbDAV's built-in repair couldn't fix (e.g. because it couldn't
match the file back to a Sonarr/Radarr media item).
"""

import json
import re
import subprocess
import time
import urllib.error

from pathlib import Path

from watchdog import config
from watchdog.api import sonarr as sonarr_api, radarr as radarr_api

log = config.logger

# ─── Module state ─────────────────────────────────────────────────────

ARTICLE_CHECK_INTERVAL = 3600
last_article_check = 0.0
_handled_ids: set = set()


# ─── Public entry point ──────────────────────────────────────────────

def check_article_health():
    """
    Periodically scan NzbDAV's HealthCheckResults for files flagged as
    unhealthy (missing articles).  For each, resolve the corresponding
    Sonarr episode or Radarr movie, delete the bad file, blocklist
    the release, and trigger a re-search.
    """
    global last_article_check
    now = time.time()
    if now - last_article_check < ARTICLE_CHECK_INTERVAL:
        return
    last_article_check = now

    try:
        result = subprocess.run(
            ["docker", "exec", "nzbdav", "sqlite3", "/config/db.sqlite",
             "SELECT json_group_array(json_object('id',Id,'path',Path,'msg',Message)) "
             "FROM HealthCheckResults WHERE Result != 0;"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            log.debug(f"NzbDAV DB query failed: {result.stderr.strip()}")
            return
        parsed = json.loads(result.stdout.strip())
        rows = [(r["id"], r["path"], r["msg"]) for r in parsed]
    except Exception as e:
        log.debug(f"NzbDAV DB read failed: {e}")
        return

    if not rows:
        return

    new_rows = [(rid, path, msg) for rid, path, msg in rows if rid not in _handled_ids]
    if not new_rows:
        return

    log.info(f"NzbDAV article check: {len(new_rows)} unhealthy file(s) to process")

    for rid, path, msg in new_rows:
        log.warning(f"  Unhealthy: {_basename(path)} — {msg}")
        resolved = _resolve_and_fix(path)
        if resolved:
            _handled_ids.add(rid)
        else:
            log.warning(f"  Could not resolve media item for: {_basename(path)}")
            _handled_ids.add(rid)


# ─── Resolution helpers ──────────────────────────────────────────────

_SE_PATTERN = re.compile(r"[Ss](\d{1,2})[Ee](\d{1,3})")


def _basename(path: str) -> str:
    return Path(path).name if path else "?"


def _resolve_and_fix(nzbdav_path: str) -> bool:
    """
    Try to match the NzbDAV path to a Sonarr episode file or Radarr movie
    file, delete it, and trigger a re-search.  Returns True if handled.
    """
    name = _basename(nzbdav_path)

    if _try_sonarr(name, nzbdav_path):
        return True
    if _try_radarr(name, nzbdav_path):
        return True
    return False


def _try_sonarr(filename: str, nzbdav_path: str) -> bool:
    """Match against Sonarr episode files by comparing scene/file names."""
    try:
        series_list = sonarr_api("GET", "/api/v3/series") or []
    except Exception:
        return False

    for series in series_list:
        title_slug = series.get("titleSlug", "")
        clean_title = series.get("cleanTitle", "")
        title = series.get("title", "")

        name_lower = filename.lower().replace(".", " ").replace("-", " ")
        if not any(t and t.lower() in name_lower for t in [
            title.replace(" ", ".").replace(":", ""),
            clean_title,
            title_slug.replace("-", " "),
            title,
        ] if t):
            continue

        try:
            ep_files = sonarr_api("GET", f"/api/v3/episodefile?seriesId={series['id']}") or []
        except Exception:
            continue

        for ef in ep_files:
            scene = ef.get("sceneName", "") or ""
            rel_path = ef.get("relativePath", "") or ""

            if filename in scene or filename in rel_path or scene in filename:
                fid = ef["id"]
                log.info(f"  Matched Sonarr file {fid}: {rel_path}")

                episode_ids = _episodes_for_file(series["id"], fid)

                try:
                    sonarr_api("DELETE", f"/api/v3/episodefile/{fid}")
                    log.info(f"  Deleted bad Sonarr file {fid}")
                except Exception as e:
                    log.error(f"  Failed to delete Sonarr file {fid}: {e}")
                    return False

                for eid in episode_ids:
                    try:
                        sonarr_api("POST", "/api/v3/command", {
                            "name": "EpisodeSearch",
                            "episodeIds": [eid],
                        })
                        log.info(f"  Re-searching episode {eid}")
                        time.sleep(config.EPISODE_SEARCH_DELAY)
                    except Exception as e:
                        log.error(f"  Re-search failed for episode {eid}: {e}")

                return True

    return False


def _episodes_for_file(series_id: int, file_id: int) -> list:
    """Return episode IDs linked to a given episode file."""
    try:
        episodes = sonarr_api("GET", f"/api/v3/episode?seriesId={series_id}") or []
        return [ep["id"] for ep in episodes if ep.get("episodeFileId") == file_id]
    except Exception:
        return []


def _try_radarr(filename: str, nzbdav_path: str) -> bool:
    """Match against Radarr movie files."""
    if not config.radarr_api_key:
        return False

    try:
        movies = radarr_api("GET", "/api/v3/movie") or []
    except Exception:
        return False

    for movie in movies:
        mf = movie.get("movieFile", {})
        if not mf:
            continue

        scene = mf.get("sceneName", "") or ""
        rel_path = mf.get("relativePath", "") or ""

        if filename in scene or filename in rel_path or scene in filename:
            movie_id = movie["id"]
            file_id = mf["id"]
            log.info(f"  Matched Radarr movie {movie['title']}: file {file_id}")

            try:
                radarr_api("DELETE", f"/api/v3/moviefile/{file_id}")
                log.info(f"  Deleted bad Radarr file {file_id}")
            except Exception as e:
                log.error(f"  Failed to delete Radarr file {file_id}: {e}")
                return False

            try:
                radarr_api("POST", "/api/v3/command", {
                    "name": "MoviesSearch",
                    "movieIds": [movie_id],
                })
                log.info(f"  Re-searching movie: {movie['title']}")
            except Exception as e:
                log.error(f"  Re-search failed for {movie['title']}: {e}")

            return True

    return False
