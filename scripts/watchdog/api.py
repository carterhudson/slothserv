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
