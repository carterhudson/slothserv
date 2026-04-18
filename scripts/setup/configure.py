#!/usr/bin/env python3
"""
Programmatic first-run configuration for Sonarr and Radarr.
Called by bootstrap.sh after containers are running.

Usage:
    python3 configure.py \
        --vm-ip <ip> \
        --dl-host <hostname> \
        --dl-api-key <key> \
        --indexer-name <name> \
        --indexer-url <url> \
        --indexer-api-key <key> \
        --indexer-tv-cats 5030,5040 \
        --indexer-anime-cats 5070 \
        --indexer-movie-cats 2000,2010,...
"""

import argparse
import json
import sys
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET
from pathlib import Path


def api(base_url, api_key, method, path, data=None, quiet=False):
    body = json.dumps(data).encode() if data else None
    req = urllib.request.Request(
        f"{base_url}{path}",
        data=body,
        headers={"X-Api-Key": api_key, "Content-Type": "application/json"},
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        if not quiet:
            body_text = ""
            try:
                body_text = e.read().decode()[:200]
            except Exception:
                pass
            print(f"  API error {e.code} on {method} {path}: {body_text}", file=sys.stderr)
        raise


def parse_csv_ints(csv_str):
    if not csv_str or not csv_str.strip():
        return []
    return [int(x.strip()) for x in csv_str.split(",") if x.strip()]


FOREIGN_DUB_REGEX = (
    r"\b(GerDub|German\.?Dub|GERMAN\.DL|FrenchDub|French\.?Dub|VOSTFR"
    r"|ITA\.?Dub|Italian\.?Dub|SPA\.?Dub|Spanish\.?Dub|POR\.?Dub"
    r"|RUS\.?Dub|DUBBiT|DUAL\.?AUDIO\.?(?:GER|FRE|SPA|ITA|POR|RUS))\b"
)


def configure_service(service, base_url, api_key, args):
    is_sonarr = service == "sonarr"
    print(f"\n  Configuring {service}...")

    # Root folders
    if is_sonarr:
        api(base_url, api_key, "POST", "/api/v3/rootfolder", {"path": "/data/media/tv"})
        api(base_url, api_key, "POST", "/api/v3/rootfolder", {"path": "/data/media/anime"})
        print(f"  [ok] root folders: /data/media/tv, /data/media/anime")
    else:
        api(base_url, api_key, "POST", "/api/v3/rootfolder", {"path": "/data/media/movies"})
        print(f"  [ok] root folder: /data/media/movies")

    # Custom formats
    foreign_dub = api(base_url, api_key, "POST", "/api/v3/customformat", {
        "name": "Foreign Dub",
        "specifications": [{
            "name": "Foreign Dub Tags",
            "implementation": "ReleaseTitleSpecification",
            "fields": [{"name": "value", "value": FOREIGN_DUB_REGEX}],
            "negate": False, "required": True,
        }],
    })

    not_orig = api(base_url, api_key, "POST", "/api/v3/customformat", {
        "name": "Not Original Language",
        "specifications": [{
            "name": "Original Language",
            "implementation": "LanguageSpecification",
            "fields": [
                {"name": "value", "value": -2},
                {"name": "exceptLanguage", "value": False},
            ],
            "negate": False, "required": True,
        }],
    })

    penalize_ids = {fd_id, no_id}

    if is_sonarr:
        season_pack = api(base_url, api_key, "POST", "/api/v3/customformat", {
            "name": "Season Pack",
            "specifications": [{
                "name": "Season Pack",
                "implementation": "ReleaseTypeSpecification",
                "fields": [{"name": "value", "value": 3}],
                "negate": False, "required": True,
            }],
        })
        penalize_ids.add(season_pack["id"])

    print(f"  [ok] custom formats (Foreign Dub={fd_id}, Not Original Language={no_id})")

    # Apply scores to quality profiles
    profiles = api(base_url, api_key, "GET", "/api/v3/qualityprofile") or []
    for p in profiles:
        changed = False
        for item in p.get("formatItems", []):
            if item.get("format", 0) in penalize_ids:
                item["score"] = -10000
                changed = True
        if changed:
            api(base_url, api_key, "PUT", f"/api/v3/qualityprofile/{p['id']}", p)
    print(f"  [ok] custom format scores applied")

    # Extra quality profiles for Sonarr
    if is_sonarr:
        profiles = api(base_url, api_key, "GET", "/api/v3/qualityprofile") or []
        hd_template = None
        for p in profiles:
            if p["name"] == "HD-1080p":
                hd_template = p
                break
        if hd_template:
            for name in ("HD 1080p+", "Anime HD 1080p+"):
                clone = dict(hd_template)
                clone.pop("id", None)
                clone["name"] = name
                api(base_url, api_key, "POST", "/api/v3/qualityprofile", clone)
            print(f"  [ok] quality profiles: HD 1080p+, Anime HD 1080p+")

    # Download client
    cat_field = "tvCategory" if is_sonarr else "movieCategory"
    cat_value = "tv" if is_sonarr else "movies"
    api(base_url, api_key, "POST", "/api/v3/downloadclient", {
        "name": "NzbDAV",
        "implementation": "Sabnzbd",
        "configContract": "SabnzbdSettings",
        "enable": True,
        "fields": [
            {"name": "host", "value": args.dl_host},
            {"name": "port", "value": 3000},
            {"name": "apiKey", "value": args.dl_api_key},
            {"name": "useSsl", "value": False},
            {"name": cat_field, "value": cat_value},
        ],
    })
    print(f"  [ok] download client: NzbDAV → {args.dl_host}")

    # Indexer
    fields = [
        {"name": "baseUrl", "value": args.indexer_url},
        {"name": "apiPath", "value": "/api"},
        {"name": "apiKey", "value": args.indexer_api_key},
    ]
    if is_sonarr:
        fields.append({"name": "categories", "value": parse_csv_ints(args.indexer_tv_cats)})
        anime_cats = parse_csv_ints(args.indexer_anime_cats)
        if anime_cats:
            fields.append({"name": "animeCategories", "value": anime_cats})
            fields.append({"name": "animeStandardFormatSearch", "value": True})
    else:
        fields.append({"name": "categories", "value": parse_csv_ints(args.indexer_movie_cats)})

    api(base_url, api_key, "POST", "/api/v3/indexer", {
        "name": args.indexer_name,
        "implementation": "Newznab",
        "configContract": "NewznabSettings",
        "enable": True,
        "fields": fields,
    })
    print(f"  [ok] indexer: {args.indexer_name}")


def main():
    parser = argparse.ArgumentParser(description="Configure Sonarr & Radarr")
    parser.add_argument("--base-dir", default=str(Path(__file__).resolve().parent.parent.parent))
    parser.add_argument("--vm-ip", default="localhost")
    parser.add_argument("--dl-host", required=True)
    parser.add_argument("--dl-api-key", required=True)
    parser.add_argument("--indexer-name", required=True)
    parser.add_argument("--indexer-url", required=True)
    parser.add_argument("--indexer-api-key", required=True)
    parser.add_argument("--indexer-tv-cats", default="5030,5040")
    parser.add_argument("--indexer-anime-cats", default="5070")
    parser.add_argument("--indexer-movie-cats", default="2000,2010,2020,2030,2040,2045,2050,2060")
    args = parser.parse_args()

    base = Path(args.base_dir)

    sonarr_key = (base / "config/api-keys/sonarr.key").read_text().strip()
    radarr_key = (base / "config/api-keys/radarr.key").read_text().strip()

    sonarr_url = f"http://{args.vm_ip}:8989"
    radarr_url = f"http://{args.vm_ip}:7878"

    configure_service("sonarr", sonarr_url, sonarr_key, args)
    configure_service("radarr", radarr_url, radarr_key, args)

    print("\n  Sonarr & Radarr configured.")
    print("  Remaining: add Plex Watchlist import list in each UI (requires Plex OAuth).")


if __name__ == "__main__":
    main()
