#!/usr/bin/env python3
"""
Quick status dashboard for the media server stack.
Shows service health, queue status, and library stats.

Usage:
  status.py           # full dashboard
  status.py --json    # machine-readable output
"""

import argparse
import json
import os
import socket
import subprocess
import sys
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent.parent
BREW_ENV = {**os.environ, "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"}

SONARR_API_KEY = ""
RADARR_API_KEY = ""
BASE_URL = "http://localhost"


def resolve_base_url():
    global BASE_URL
    try:
        result = subprocess.run(
            ["/opt/homebrew/bin/colima", "ls", "--json"],
            capture_output=True, text=True, timeout=10, env=BREW_ENV,
        )
        if result.returncode == 0:
            for line in result.stdout.strip().splitlines():
                info = json.loads(line)
                if info.get("status") == "Running":
                    addr = info.get("address", "")
                    if addr:
                        BASE_URL = f"http://{addr}"
                        return
    except Exception:
        pass


def load_api_keys():
    global SONARR_API_KEY, RADARR_API_KEY
    try:
        tree = ET.parse(str(BASE_DIR / "config/sonarr/config.xml"))
        SONARR_API_KEY = tree.find("ApiKey").text
    except Exception:
        pass
    try:
        tree = ET.parse(str(BASE_DIR / "config/radarr/config.xml"))
        RADARR_API_KEY = tree.find("ApiKey").text
    except Exception:
        pass


def http_get(url, api_key=None, timeout=5):
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    if api_key:
        headers["X-Api-Key"] = api_key
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def docker_health(container):
    """Check container state and health via docker inspect."""
    try:
        fmt = '{{.State.Status}}|{{if .State.Health}}{{.State.Health.Status}}{{end}}'
        result = subprocess.run(
            ["docker", "inspect", "--format", fmt, container],
            capture_output=True, text=True, timeout=5, env=BREW_ENV,
        )
        if result.returncode != 0:
            return {"status": "down"}
        parts = result.stdout.strip().split("|", 1)
        state = parts[0]
        health = parts[1] if len(parts) > 1 else ""
        if state == "running":
            if health == "healthy":
                return {"status": "up"}
            elif health == "unhealthy":
                return {"status": "degraded", "note": "unhealthy"}
            elif health == "starting":
                return {"status": "degraded", "note": "starting"}
            return {"status": "up"}
        elif state == "restarting":
            return {"status": "degraded", "note": "restarting"}
        elif state:
            return {"status": "down", "note": state}
    except Exception:
        pass
    return {"status": "down"}


def check_service(name, port, api_key=None, path="/"):
    try:
        url = f"{BASE_URL}:{port}{path}"
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if api_key:
            headers["X-Api-Key"] = api_key
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=5) as r:
            return {"status": "up", "code": r.status}
    except urllib.error.HTTPError as e:
        return {"status": "up", "code": e.code}
    except Exception:
        pass

    return docker_health(name)


SERVICES = [
    ("Plex",       "plex",       32400, None,            "/identity"),
    ("Sonarr",     "sonarr",     8989,  "sonarr",        "/api/v3/health"),
    ("Radarr",     "radarr",     7878,  "radarr",        "/api/v3/health"),
    ("NzbDAV",     "nzbdav",     None,  None,            None),
    ("Bazarr",     "bazarr",     6767,  None,            "/"),
    ("Tautulli",   "tautulli",   8181,  None,            "/"),
    ("Seerr",      "seerr",      5055,  None,            "/api/v1/status"),
    ("Recyclarr",  "recyclarr",  None,  None,            None),
    ("Watchtower", "watchtower", None,  None,            None),
]


def get_sonarr_stats():
    try:
        series = http_get(f"{BASE_URL}:8989/api/v3/series", SONARR_API_KEY, timeout=10)
        queue = http_get(f"{BASE_URL}:8989/api/v3/queue?pageSize=1", SONARR_API_KEY)
        health = http_get(f"{BASE_URL}:8989/api/v3/health", SONARR_API_KEY)

        total_eps = sum(s.get("statistics", {}).get("episodeCount", 0) for s in series)
        have_eps = sum(s.get("statistics", {}).get("episodeFileCount", 0) for s in series)

        return {
            "series": len(series),
            "episodes": total_eps,
            "have": have_eps,
            "missing": total_eps - have_eps,
            "queue": queue.get("totalRecords", 0),
            "warnings": len([h for h in health if h.get("type") == "warning"]),
            "errors": len([h for h in health if h.get("type") == "error"]),
            "health_details": [h.get("message", "") for h in health],
        }
    except Exception as e:
        return {"error": str(e)}


def get_radarr_stats():
    if not RADARR_API_KEY:
        return {"error": "no API key"}
    try:
        movies = http_get(f"{BASE_URL}:7878/api/v3/movie", RADARR_API_KEY, timeout=10)
        queue = http_get(f"{BASE_URL}:7878/api/v3/queue?pageSize=1", RADARR_API_KEY)

        have = sum(1 for m in movies if m.get("hasFile"))

        return {
            "movies": len(movies),
            "have": have,
            "missing": len(movies) - have,
            "queue": queue.get("totalRecords", 0),
        }
    except Exception as e:
        return {"error": str(e)}


def get_watchdog_status():
    try:
        result = subprocess.run(
            ["launchctl", "list", "com.slothserv.watchdog"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                if '"PID"' in line:
                    return "running"
            return "loaded"
        return "not loaded"
    except Exception:
        return "unknown"


def print_dashboard():
    resolve_base_url()
    load_api_keys()

    print("=" * 55)
    print("  SLOTHSERV STATUS")
    print("=" * 55)

    print("\n  Services:")
    for label, container, port, key_source, path in SERVICES:
        if port and path:
            api_key = SONARR_API_KEY if key_source == "sonarr" else RADARR_API_KEY if key_source == "radarr" else None
            result = check_service(container, port, api_key, path)
        else:
            result = docker_health(container)

        status = result["status"]
        icon = "OK" if status == "up" else ("WARN" if status == "degraded" else "DOWN")
        note = f" ({result['note']})" if result.get("note") else ""
        print(f"    {label:12s}  [{icon:4s}]{note}")

    wd = get_watchdog_status()
    wd_icon = "OK" if wd == "running" else "WARN" if wd == "loaded" else "DOWN"
    print(f"    {'Watchdog':12s}  [{wd_icon:4s}] {wd}")

    print("\n  Sonarr Library:")
    stats = get_sonarr_stats()
    if "error" in stats:
        print(f"    Could not fetch: {stats['error']}")
    else:
        print(f"    Series:    {stats['series']}")
        print(f"    Episodes:  {stats['have']}/{stats['episodes']} ({stats['missing']} missing)")
        print(f"    Queue:     {stats['queue']} item(s)")
        if stats["warnings"] or stats["errors"]:
            print(f"    Health:    {stats['errors']} error(s), {stats['warnings']} warning(s)")
            for msg in stats["health_details"]:
                print(f"      - {msg[:70]}")
        else:
            print(f"    Health:    OK")

    print("\n  Radarr Library:")
    rstats = get_radarr_stats()
    if "error" in rstats:
        print(f"    Could not fetch: {rstats['error']}")
    else:
        print(f"    Movies:    {rstats['movies']}")
        print(f"    Have:      {rstats['have']}/{rstats['movies']} ({rstats['missing']} missing)")
        print(f"    Queue:     {rstats['queue']} item(s)")

    print("\n  Quick Links:")
    hostname = socket.gethostname()
    if not hostname.endswith(".local"):
        hostname += ".local"
    print(f"    Plex:      http://{hostname}:32400/web")
    print(f"    Sonarr:    http://{hostname}:8989")
    print(f"    Radarr:    http://{hostname}:7878")
    print(f"    Bazarr:    http://{hostname}:6767")
    print(f"    Tautulli:  http://{hostname}:8181")
    print(f"    Seerr:     http://{hostname}:5055")
    print("=" * 55)


def print_json():
    resolve_base_url()
    load_api_keys()

    data = {
        "services": {},
        "watchdog": get_watchdog_status(),
        "sonarr": get_sonarr_stats(),
        "radarr": get_radarr_stats(),
    }
    for label, container, port, key_source, path in SERVICES:
        if port and path:
            api_key = SONARR_API_KEY if key_source == "sonarr" else RADARR_API_KEY if key_source == "radarr" else None
            data["services"][label] = check_service(container, port, api_key, path)
        else:
            data["services"][label] = docker_health(container)

    print(json.dumps(data, indent=2))


def main():
    parser = argparse.ArgumentParser(description="SlothServ status dashboard")
    parser.add_argument("--json", "-j", action="store_true", help="JSON output")
    args = parser.parse_args()

    if args.json:
        print_json()
    else:
        print_dashboard()


if __name__ == "__main__":
    main()
