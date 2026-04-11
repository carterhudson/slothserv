#!/usr/bin/env python3
"""
Searches for episodes one at a time instead of Sonarr's default batch season search.
This avoids the problem where Sonarr evaluates all episodes in a season before grabbing
any of them, causing long waits with NzbDAV's instant streaming.

Usage:
  episode-search.py <series-name>                # search all missing episodes
  episode-search.py <series-name> --season 2     # search missing in season 2
  episode-search.py --list                        # list all series with missing episodes
"""

import argparse
import json
import sys
import time
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent.parent
SONARR_URL = "http://localhost:8989"
SONARR_API_KEY = ""
GRAB_DELAY = 3  # seconds between episode searches to avoid hammering indexers


def api(method, path, data=None):
    url = f"{SONARR_URL}{path}"
    body = json.dumps(data).encode() if data else None
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "X-Api-Key": SONARR_API_KEY,
            "Content-Type": "application/json",
        },
        method=method,
    )
    with urllib.request.urlopen(req) as r:
        return json.load(r) if r.status != 204 else None


def load_api_key():
    global SONARR_API_KEY
    tree = ET.parse(str(BASE_DIR / "config/sonarr/config.xml"))
    SONARR_API_KEY = tree.find("ApiKey").text


def find_series(name):
    series_list = api("GET", "/api/v3/series")
    name_lower = name.lower()
    matches = [s for s in series_list if name_lower in s["title"].lower()]
    if not matches:
        print(f"No series matching '{name}'")
        sys.exit(1)
    if len(matches) > 1:
        print(f"Multiple matches for '{name}':")
        for s in matches:
            print(f"  [{s['id']}] {s['title']}")
        sys.exit(1)
    return matches[0]


def get_missing_episodes(series_id, season=None):
    episodes = api("GET", f"/api/v3/episode?seriesId={series_id}")
    missing = []
    for ep in episodes:
        if not ep.get("monitored", True):
            continue
        if ep.get("hasFile"):
            continue
        if season is not None and ep["seasonNumber"] != season:
            continue
        if ep["seasonNumber"] == 0:
            continue
        missing.append(ep)
    return sorted(missing, key=lambda e: (e["seasonNumber"], e["episodeNumber"]))


def search_episode(episode_id):
    return api("POST", "/api/v3/command", {
        "name": "EpisodeSearch",
        "episodeIds": [episode_id],
    })


def list_missing():
    series_list = api("GET", "/api/v3/series")
    any_missing = False
    for s in sorted(series_list, key=lambda x: x["title"]):
        stats = s.get("statistics", {})
        total = stats.get("episodeCount", 0)
        have = stats.get("episodeFileCount", 0)
        missing = total - have
        if missing > 0:
            any_missing = True
            print(f"  {s['title']}: {missing} missing / {total} total")
    if not any_missing:
        print("  No missing episodes across any series!")


def main():
    parser = argparse.ArgumentParser(description="Search episodes one-by-one")
    parser.add_argument("series", nargs="?", help="Series name (partial match)")
    parser.add_argument("--season", "-s", type=int, help="Limit to a specific season")
    parser.add_argument("--list", "-l", action="store_true", help="List series with missing episodes")
    parser.add_argument("--dry-run", "-n", action="store_true", help="Show what would be searched without doing it")
    args = parser.parse_args()

    load_api_key()

    if args.list:
        print("Series with missing episodes:")
        list_missing()
        return

    if not args.series:
        parser.print_help()
        return

    series = find_series(args.series)
    print(f"Series: {series['title']} (ID: {series['id']})")

    missing = get_missing_episodes(series["id"], args.season)
    if not missing:
        season_msg = f" season {args.season}" if args.season else ""
        print(f"  No missing episodes{season_msg}!")
        return

    print(f"  {len(missing)} missing episode(s)")

    for i, ep in enumerate(missing):
        label = f"S{ep['seasonNumber']:02d}E{ep['episodeNumber']:02d} - {ep['title']}"
        if args.dry_run:
            print(f"  [dry-run] would search: {label}")
            continue

        print(f"  [{i+1}/{len(missing)}] Searching: {label}")
        try:
            result = search_episode(ep["id"])
            print(f"    -> {result.get('status', '?')}")
        except Exception as e:
            print(f"    -> error: {e}")

        if i < len(missing) - 1:
            time.sleep(GRAB_DELAY)

    if not args.dry_run:
        print("Done! Check Sonarr queue for results.")


if __name__ == "__main__":
    main()
