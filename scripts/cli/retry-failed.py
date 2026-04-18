#!/usr/bin/env python3
"""
Checks Sonarr's queue for failed downloads and retries them automatically.
Can run once or in a loop (--watch mode).

Usage:
  retry-failed.py              # check once, retry any failures
  retry-failed.py --watch      # loop, checking every 5 minutes
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
CHECK_INTERVAL = 300


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
    SONARR_API_KEY = (BASE_DIR / "config/api-keys/sonarr.key").read_text().strip()


def get_failed_items():
    queue = api("GET", "/api/v3/queue?pageSize=200&includeUnknownSeriesItems=true")
    failed = []
    for item in queue.get("records", []):
        state = item.get("trackedDownloadState", "")
        status = item.get("trackedDownloadStatus", "")
        if state == "importFailed" or status == "error":
            failed.append(item)
    return failed


def retry_item(item):
    item_id = item["id"]
    title = item.get("title", "unknown")[:70]
    print(f"  retrying: {title}")
    try:
        # Remove the failed item (blocklist=false so it can be re-grabbed)
        req = urllib.request.Request(
            f"{SONARR_URL}/api/v3/queue/{item_id}?removeFromClient=true&blocklist=false&skipRedownload=false",
            headers={
                "X-Api-Key": SONARR_API_KEY,
                "Content-Type": "application/json",
            },
            method="DELETE",
        )
        urllib.request.urlopen(req)
        print(f"    -> removed, Sonarr will re-search automatically")
        return True
    except Exception as e:
        print(f"    -> error: {e}")
        return False


def check_once():
    failed = get_failed_items()
    if not failed:
        print("No failed items in queue.")
        return 0

    print(f"Found {len(failed)} failed item(s):")
    retried = 0
    for item in failed:
        if retry_item(item):
            retried += 1
            time.sleep(1)

    print(f"Retried {retried}/{len(failed)} item(s).")

    # Trigger a search for missing episodes to re-grab
    if retried > 0:
        series_ids = set()
        for item in failed:
            sid = item.get("seriesId")
            if sid:
                series_ids.add(sid)

        for sid in series_ids:
            print(f"  triggering search for series {sid}...")
            try:
                api("POST", "/api/v3/command", {
                    "name": "SeriesSearch",
                    "seriesId": sid,
                })
            except Exception as e:
                print(f"    -> error: {e}")

    return retried


def main():
    parser = argparse.ArgumentParser(description="Retry failed Sonarr downloads")
    parser.add_argument("--watch", "-w", action="store_true", help="Run continuously")
    args = parser.parse_args()

    load_api_key()

    if args.watch:
        print(f"Watching for failed downloads (checking every {CHECK_INTERVAL}s)")
        while True:
            try:
                check_once()
                time.sleep(CHECK_INTERVAL)
            except urllib.error.URLError:
                print("Sonarr not reachable, retrying in 30s...")
                time.sleep(30)
            except KeyboardInterrupt:
                print("\nStopped.")
                break
    else:
        check_once()


if __name__ == "__main__":
    main()
