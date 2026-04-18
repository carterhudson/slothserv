#!/usr/bin/env python3
"""
Export the running SlothServ stack's credentials and configuration
into a portable JSON file that bootstrap.sh --import can consume.

Usage:
    python3 export-config.py [-o slothserv-config.json]

What it captures:
  - Timezone
  - NzbDAV API key + WebDAV password (obscured, from rclone.conf)
  - Indexer name, URL, API key, and category IDs (from Sonarr API)
  - VPN enabled flag + all WireGuard config files
  - Sonarr/Radarr custom format names (for reference, not re-applied)

What it does NOT capture (must be re-entered on fresh install):
  - Plex claim token (one-time, expires in 4 minutes)
  - NzbDAV Usenet provider credentials (stored in NzbDAV's own DB)
  - Plex server identity / Preferences.xml
"""

import argparse
import json
import os
import sys
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path


def api(base_url, api_key, path):
    req = urllib.request.Request(
        f"{base_url}{path}",
        headers={"X-Api-Key": api_key, "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


def get_field(fields, name):
    for f in fields:
        if f.get("name") == name:
            return f.get("value", "")
    return ""


def main():
    parser = argparse.ArgumentParser(description="Export SlothServ config")
    parser.add_argument("-o", "--output", default="slothserv-config.json")
    parser.add_argument("--base-dir", default=str(Path(__file__).resolve().parent.parent.parent))
    args = parser.parse_args()

    base = Path(args.base_dir)

    # Resolve VM IP
    vm_ip = "localhost"
    try:
        import subprocess
        result = subprocess.run(
            ["/opt/homebrew/bin/colima", "ls", "--json"],
            capture_output=True, text=True, timeout=10,
            env={**os.environ, "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"},
        )
        if result.returncode == 0:
            for line in result.stdout.strip().splitlines():
                info = json.loads(line)
                if info.get("status") == "Running":
                    addr = info.get("address", "")
                    if addr:
                        vm_ip = addr
                        break
    except Exception:
        pass

    # .env
    env_vars = {}
    env_path = base / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env_vars[k.strip()] = v.strip()

    tz = env_vars.get("TZ", "America/New_York")

    # Sonarr API key
    sonarr_key = ""
    try:
        sonarr_key = (base / "config/api-keys/sonarr.key").read_text().strip()
    except Exception:
        print("Error: Cannot read Sonarr API key", file=sys.stderr)
        sys.exit(1)

    sonarr_url = f"http://{vm_ip}:8989"

    # Indexer from Sonarr
    indexer_config = {}
    try:
        indexers = api(sonarr_url, sonarr_key, "/api/v3/indexer")
        if indexers:
            idx = indexers[0]
            fields = idx.get("fields", [])
            indexer_config = {
                "name": idx.get("name", ""),
                "url": get_field(fields, "baseUrl"),
                "api_key": get_field(fields, "apiKey"),
                "tv_categories": ",".join(str(c) for c in (get_field(fields, "categories") or [])),
                "anime_categories": ",".join(str(c) for c in (get_field(fields, "animeCategories") or [])),
            }
    except Exception as e:
        print(f"Warning: Could not read indexer config: {e}", file=sys.stderr)

    # Movie categories from Radarr
    try:
        radarr_key = ET.parse(str(base / "config/radarr/config.xml")).find("ApiKey").text
        radarr_url = f"http://{vm_ip}:7878"
        radarr_indexers = api(radarr_url, radarr_key, "/api/v3/indexer")
        if radarr_indexers:
            fields = radarr_indexers[0].get("fields", [])
            indexer_config["movie_categories"] = ",".join(
                str(c) for c in (get_field(fields, "categories") or [])
            )
    except Exception:
        pass

    # NzbDAV API key from Sonarr's download client
    nzbdav_api_key = ""
    try:
        clients = api(sonarr_url, sonarr_key, "/api/v3/downloadclient")
        for c in clients:
            if c.get("implementation") == "Sabnzbd":
                nzbdav_api_key = get_field(c.get("fields", []), "apiKey")
                break
    except Exception:
        pass

    # WebDAV password from rclone.conf (obscured form)
    rclone_pass = ""
    rclone_conf = base / "rclone.conf"
    if rclone_conf.exists():
        for line in rclone_conf.read_text().splitlines():
            if line.strip().startswith("pass ="):
                rclone_pass = line.split("=", 1)[1].strip()

    # VPN: check if gluetun is in docker-compose, grab WireGuard configs
    vpn_enabled = False
    compose_path = base / "docker-compose.yml"
    if compose_path.exists():
        vpn_enabled = "gluetun" in compose_path.read_text()

    wg_configs = {}
    wg_dir = base / "config" / "gluetun" / "wireguard"
    if wg_dir.is_dir():
        for conf in sorted(wg_dir.glob("*.conf")):
            content = conf.read_text().strip()
            if content and "PLACEHOLDER" not in content:
                wg_configs[conf.name] = content

    # Build export
    export = {
        "version": 1,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "general": {
            "timezone": tz,
        },
        "nzbdav": {
            "api_key": nzbdav_api_key,
            "webdav_password_obscured": rclone_pass,
        },
        "indexer": indexer_config,
        "vpn": {
            "enabled": vpn_enabled,
            "wireguard_configs": wg_configs,
        },
    }

    output_path = Path(args.output)
    output_path.write_text(json.dumps(export, indent=2) + "\n")

    # Summary
    print(f"\nExported to: {output_path.resolve()}")
    print(f"  Timezone:     {tz}")
    print(f"  Indexer:      {indexer_config.get('name', '?')}")
    print(f"  NzbDAV key:   {'yes' if nzbdav_api_key else 'missing'}")
    print(f"  WebDAV pass:  {'yes (obscured)' if rclone_pass else 'missing'}")
    print(f"  VPN:          {'enabled' if vpn_enabled else 'disabled'}")
    print(f"  WG configs:   {len(wg_configs)} file(s)")
    print()
    print("This file contains secrets. Keep it safe.")
    print("To restore on a new machine:")
    print(f"  bash bootstrap.sh --import {output_path.name}")


if __name__ == "__main__":
    main()
