"""
Thin HTTP helpers for Sonarr, Radarr, and Plex APIs.
"""

import json
import urllib.request

from watchdog import config


def sonarr(method, path, data=None, timeout=30):
    url = f"{config.sonarr_url}{path}"
    body = json.dumps(data).encode() if data else None
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "X-Api-Key": config.sonarr_api_key,
            "Content-Type": "application/json",
        },
        method=method,
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        if r.status == 204:
            return None
        raw = r.read()
        return json.loads(raw) if raw else None


def radarr(method, path, data=None, timeout=30):
    if not config.radarr_api_key:
        return None
    url = f"{config.radarr_url}{path}"
    body = json.dumps(data).encode() if data else None
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "X-Api-Key": config.radarr_api_key,
            "Content-Type": "application/json",
        },
        method=method,
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        if r.status == 204:
            return None
        raw = r.read()
        return json.loads(raw) if raw else None


def plex(path):
    if not config.plex_token:
        return None
    sep = "&" if "?" in path else "?"
    req = urllib.request.Request(
        f"{config.plex_url}{path}{sep}X-Plex-Token={config.plex_token}",
        headers={"Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


def plex_watchlist():
    """Fetch the full Plex watchlist with external GUIDs, paginating as needed.
    Returns a list of Metadata dicts (possibly empty), or None if Plex isn't configured.
    Raises on transport errors so callers can distinguish 'empty watchlist' from 'API down'."""
    if not config.plex_token:
        return None
    items: list = []
    start = 0
    size = 100
    while True:
        req = urllib.request.Request(
            "https://discover.provider.plex.tv/library/sections/watchlist/all?includeGuids=1",
            headers={
                "Accept": "application/json",
                "X-Plex-Token": config.plex_token,
                "X-Plex-Client-Identifier": "slothserv-watchdog",
                "X-Plex-Product": "SlothServ",
                "X-Plex-Container-Start": str(start),
                "X-Plex-Container-Size": str(size),
            },
        )
        with urllib.request.urlopen(req, timeout=30) as r:
            mc = (json.loads(r.read()) or {}).get("MediaContainer") or {}
        batch = mc.get("Metadata") or []
        items.extend(batch)
        total = mc.get("totalSize", len(items))
        if len(batch) < size or len(items) >= total:
            break
        start += size
    return items
