"""
Colima VM IP resolution and connectivity failover.
"""

import json
import subprocess
import time
import urllib.request

from pathlib import Path

from watchdog import config

log = config.logger


def resolve_colima_ip():
    """
    Get the Colima VM's routable IP address. This bypasses port forwarding
    entirely — the VM IP is directly reachable from the host via the macOS
    Virtualization.framework shared network.
    """
    try:
        result = subprocess.run(
            [config.COLIMA_BIN, "ls", "--json"],
            capture_output=True, text=True, timeout=10, env=config.BREW_ENV,
        )
        if result.returncode == 0:
            for line in result.stdout.strip().splitlines():
                info = json.loads(line)
                if info.get("status") == "Running":
                    addr = info.get("address", "")
                    if addr:
                        return addr
    except Exception:
        pass
    return ""


def init_service_urls():
    """
    Try to reach services via the Colima VM IP directly, falling back to
    localhost. Using the VM IP makes the watchdog immune to port forwarding
    failures.
    """
    vm_ip = resolve_colima_ip()
    if not vm_ip:
        log.info("Could not resolve Colima VM IP — using localhost")
        return

    try:
        req = urllib.request.Request(f"http://{vm_ip}:8989/ping",
                                     headers={"X-Api-Key": config.sonarr_api_key})
        urllib.request.urlopen(req, timeout=5)
        config.sonarr_url = f"http://{vm_ip}:8989"
        config.radarr_url = f"http://{vm_ip}:7878"
        config.plex_url = f"http://{vm_ip}:32400"
        log.info(f"Using Colima VM IP directly: {vm_ip} (bypasses port forwarding)")
        return
    except Exception:
        pass

    log.info(f"Colima VM IP {vm_ip} not reachable yet — using localhost")


def check_connectivity():
    """
    Verify we can reach Sonarr. If the current URL fails, try switching
    to the VM IP (immune to port forwarding failures). Only restart
    Colima + stack as a last resort when even the VM IP is unreachable.
    """
    # Can we reach Sonarr on the current URL?
    try:
        req = urllib.request.Request(
            f"{config.sonarr_url}/ping",
            headers={"X-Api-Key": config.sonarr_api_key},
        )
        urllib.request.urlopen(req, timeout=5)
        return
    except Exception:
        pass

    # Current URL failed — try the VM IP as failover
    vm_ip = resolve_colima_ip()
    if vm_ip:
        try:
            req = urllib.request.Request(
                f"http://{vm_ip}:8989/ping",
                headers={"X-Api-Key": config.sonarr_api_key},
            )
            urllib.request.urlopen(req, timeout=5)
            config.sonarr_url = f"http://{vm_ip}:8989"
            config.radarr_url = f"http://{vm_ip}:7878"
            config.plex_url = f"http://{vm_ip}:32400"
            log.warning(f"Port forwarding broken — switched to VM IP {vm_ip}")
            return
        except Exception:
            pass

    # Neither localhost nor VM IP works — check if the container is running at all
    try:
        result = subprocess.run(
            ["docker", "inspect", "--format", "{{.State.Status}}", "sonarr"],
            capture_output=True, text=True, timeout=10, env=config.BREW_ENV,
        )
        if result.stdout.strip() != "running":
            return
    except Exception:
        return

    # Container running but completely unreachable — nuclear restart
    log.warning("Sonarr unreachable via localhost and VM IP — restarting Colima")

    try:
        subprocess.run(
            ["docker", "compose", "down"],
            cwd=str(config.BASE_DIR), capture_output=True, timeout=60, env=config.BREW_ENV,
        )
        log.info("  Containers stopped")

        subprocess.run(
            [config.COLIMA_BIN, "restart"],
            capture_output=True, timeout=120, env=config.BREW_ENV,
        )
        log.info("  Colima restarted")

        subprocess.run(
            ["docker", "compose", "up", "-d"],
            cwd=str(config.BASE_DIR), capture_output=True, timeout=60, env=config.BREW_ENV,
        )
        log.info("  Containers restarted")

        time.sleep(30)
        init_service_urls()

    except subprocess.TimeoutExpired:
        log.error("  Recovery timed out — manual intervention may be needed (run: mrestart)")
    except Exception as e:
        log.error(f"  Recovery failed: {e}")
