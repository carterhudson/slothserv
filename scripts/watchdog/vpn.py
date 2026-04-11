"""
VPN health monitoring and automatic server failover via gluetun.
"""

import random
import shutil
import subprocess
import time

from watchdog import config

log = config.logger

# ─── Module state ─────────────────────────────────────────────────────

last_vpn_check = 0.0
vpn_consecutive_failures = 0


# ─── Public entry point ──────────────────────────────────────────────

def check_health():
    """
    Monitor gluetun's Docker health status. If the VPN tunnel is down for
    VPN_UNHEALTHY_THRESHOLD consecutive checks, rotate to a different
    WireGuard server and restart gluetun.
    """
    global last_vpn_check, vpn_consecutive_failures

    now = time.time()
    if now - last_vpn_check < config.VPN_CHECK_INTERVAL:
        return
    last_vpn_check = now

    healthy = False
    try:
        result = subprocess.run(
            ["docker", "inspect", "--format", "{{.State.Health.Status}}", "gluetun"],
            capture_output=True, text=True, timeout=10, env=config.BREW_ENV,
        )
        if result.stdout.strip() == "healthy":
            healthy = True
    except Exception:
        pass

    if healthy:
        if vpn_consecutive_failures > 0:
            log.info(f"VPN recovered after {vpn_consecutive_failures} failed check(s)")
        vpn_consecutive_failures = 0
        return

    vpn_consecutive_failures += 1
    log.warning(f"VPN unhealthy ({vpn_consecutive_failures}/{config.VPN_UNHEALTHY_THRESHOLD})")

    if vpn_consecutive_failures < config.VPN_UNHEALTHY_THRESHOLD:
        return

    vpn_consecutive_failures = 0
    _rotate_server()


# ─── Server rotation ─────────────────────────────────────────────────

def _rotate_server():
    """Pick a different WireGuard server config and restart gluetun."""
    wg_dir = config.BASE_DIR / "config" / "gluetun" / "wireguard"
    active_conf = wg_dir / "wg0.conf"

    pool = sorted(wg_dir.glob("us-*.conf"))
    if not pool:
        log.error("No VPN server pool configs found — cannot rotate")
        return

    current_endpoint = ""
    try:
        for line in active_conf.read_text().splitlines():
            if line.strip().startswith("Endpoint"):
                current_endpoint = line.split("=", 1)[1].strip()
                break
    except Exception:
        pass

    candidates = []
    for conf in pool:
        try:
            text = conf.read_text()
            for line in text.splitlines():
                if line.strip().startswith("Endpoint"):
                    ep = line.split("=", 1)[1].strip()
                    if ep != current_endpoint:
                        candidates.append(conf)
                    break
        except Exception:
            continue

    if not candidates:
        candidates = pool

    chosen = random.choice(candidates)
    log.info(f"Rotating VPN server: {chosen.stem}")

    shutil.copy2(str(chosen), str(active_conf))

    try:
        subprocess.run(
            ["docker", "compose", "restart", "gluetun"],
            cwd=str(config.BASE_DIR), capture_output=True, timeout=60, env=config.BREW_ENV,
        )
        time.sleep(15)
        log.info(f"  gluetun restarted with {chosen.stem}")
    except Exception as e:
        log.error(f"  Failed to restart gluetun: {e}")
