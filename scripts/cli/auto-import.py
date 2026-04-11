#!/usr/bin/env python3
"""
Watches Sonarr's queue for completed downloads stuck with the
"matched to series by ID" warning and auto-imports them.

Runs in a loop, checking every 60 seconds.
"""

import json
import time
import sys
import urllib.request
import urllib.error
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent.parent
SONARR_URL = "http://localhost:8989"
SONARR_API_KEY = ""
CHECK_INTERVAL = 60

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

def get_stuck_items():
    queue = api("GET", "/api/v3/queue?pageSize=100&includeUnknownSeriesItems=true")
    stuck = []
    for item in queue.get("records", []):
        if item.get("trackedDownloadStatus") != "warning":
            continue
        for msg in item.get("statusMessages", []):
            for m in msg.get("messages", []):
                if "matched to series by ID" in m:
                    stuck.append(item)
                    break
    return stuck

def try_import(item):
    title = item.get("title", "unknown")[:60]
    series_id = item.get("seriesId")
    episode_id = item.get("episodeId")
    download_id = item.get("downloadId")

    if not series_id or not episode_id:
        print(f"  skip {title}: missing series/episode ID")
        return False

    output_path = item.get("outputPath", "")
    if not output_path:
        print(f"  skip {title}: no output path")
        return False

    print(f"  importing {title}...")

    scan = api("GET", f"/api/v3/manualimport?downloadId={download_id}&seriesId={series_id}&filterExistingFiles=false")

    files_to_import = []
    for f in scan:
        if not f.get("series") or not f.get("episodes"):
            continue
        files_to_import.append({
            "path": f["path"],
            "seriesId": f["series"]["id"],
            "episodeIds": [e["id"] for e in f["episodes"]],
            "quality": f["quality"],
            "languages": f.get("languages", [{"id": 1, "name": "English"}]),
            "indexerFlags": 0,
            "releaseType": "singleEpisode",
            "downloadId": download_id,
        })

    if not files_to_import:
        print(f"  skip {title}: no importable files found")
        return False

    result = api("POST", "/api/v3/command", {
        "name": "ManualImport",
        "files": files_to_import,
    })
    print(f"  queued import for {len(files_to_import)} file(s): {result.get('status', '?')}")
    return True

def load_api_key():
    global SONARR_API_KEY
    try:
        import xml.etree.ElementTree as ET
        tree = ET.parse(str(BASE_DIR / "config/sonarr/config.xml"))
        SONARR_API_KEY = tree.find("ApiKey").text
        return True
    except Exception as e:
        print(f"Could not read Sonarr API key: {e}")
        return False

def main():
    if not load_api_key():
        sys.exit(1)
    print(f"Sonarr auto-import watchdog started (checking every {CHECK_INTERVAL}s)")

    while True:
        try:
            stuck = get_stuck_items()
            if stuck:
                print(f"Found {len(stuck)} stuck item(s)")
                for item in stuck:
                    try:
                        try_import(item)
                    except Exception as e:
                        print(f"  error: {e}")
            time.sleep(CHECK_INTERVAL)
        except urllib.error.URLError:
            print("Sonarr not reachable, retrying in 30s...")
            time.sleep(30)
        except KeyboardInterrupt:
            print("\nStopped.")
            break

if __name__ == "__main__":
    main()
