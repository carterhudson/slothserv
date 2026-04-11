"""
Nightly config backup: tarballs the service config directories and
rotates old backups.
"""

import shutil
import subprocess
import tarfile
import time

from datetime import datetime
from pathlib import Path

from watchdog import config

log = config.logger

# ─── Constants ────────────────────────────────────────────────────────

BACKUP_INTERVAL = 24 * 3600
BACKUP_DIR = config.BASE_DIR / "backups"
MAX_BACKUPS = 7

BACKUP_TARGETS = [
    "config/sonarr/config.xml",
    "config/radarr/config.xml",
    "config/sonarr/sonarr.db",
    "config/radarr/radarr.db",
    "config/bazarr/config/config.yaml",
    "config/tautulli/config.ini",
    "config/recyclarr/recyclarr.yml",
    "config/recyclarr/secrets.yml",
    "config/plex/Library/Application Support/Plex Media Server/Preferences.xml",
    "docker-compose.yml",
]

DOCKER_BACKUP_TARGETS = [
    ("nzbdav", "/config/db.sqlite", "config/nzbdav/db.sqlite"),
]

# ─── Module state ─────────────────────────────────────────────────────

last_backup = 0.0


# ─── Public entry point ──────────────────────────────────────────────

def backup_configs():
    global last_backup
    now = time.time()
    if now - last_backup < BACKUP_INTERVAL:
        return
    last_backup = now

    BACKUP_DIR.mkdir(exist_ok=True)

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    archive = BACKUP_DIR / f"slothserv-{stamp}.tar.gz"

    try:
        count = 0
        with tarfile.open(str(archive), "w:gz") as tar:
            for rel in BACKUP_TARGETS:
                src = config.BASE_DIR / rel
                if src.exists():
                    tar.add(str(src), arcname=rel)
                    count += 1

            for container, src_path, arcname in DOCKER_BACKUP_TARGETS:
                tmp = BACKUP_DIR / f".{container}_backup.tmp"
                try:
                    subprocess.run(
                        ["docker", "cp", f"{container}:{src_path}", str(tmp)],
                        capture_output=True, timeout=30,
                    )
                    if tmp.exists():
                        tar.add(str(tmp), arcname=arcname)
                        count += 1
                        tmp.unlink()
                except Exception:
                    pass

        size_mb = archive.stat().st_size / 1e6
        log.info(f"Config backup: {archive.name} ({count} files, {size_mb:.1f} MB)")
    except Exception as e:
        log.error(f"Config backup failed: {e}")
        archive.unlink(missing_ok=True)
        return

    _rotate_backups()


def _rotate_backups():
    backups = sorted(BACKUP_DIR.glob("slothserv-*.tar.gz"), reverse=True)
    for old in backups[MAX_BACKUPS:]:
        try:
            old.unlink()
            log.info(f"  Rotated old backup: {old.name}")
        except Exception:
            pass
