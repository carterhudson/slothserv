#!/usr/bin/env bash
#
# SlothServ Bootstrap
# Sets up the entire media server stack on a fresh macOS machine.
#
# Usage:
#   bash <(curl -fsSL https://raw.githubusercontent.com/carterhudson/slothserv/main/bootstrap.sh)
#   bash bootstrap.sh                      # Interactive — prompts for everything
#   bash bootstrap.sh --import config.json # Headless — reads saved credentials
#   bash bootstrap.sh --export config.json # Snapshot — dumps live stack to file
#
# Everything is prompted interactively — no hardcoded providers, indexers,
# or VPN services. Bring your own Newznab indexer, download client, and VPN.
#
set -euo pipefail

IMPORT_FILE=""
EXPORT_FILE=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --import) IMPORT_FILE="$2"; shift 2 ;;
        --export) EXPORT_FILE="$2"; shift 2 ;;
        *) echo "Unknown option: $1"; exit 1 ;;
    esac
done

# ─── Helpers ──────────────────────────────────────────────────────────

RED='\033[0;31m'  GREEN='\033[0;32m'  YELLOW='\033[1;33m'
CYAN='\033[0;36m' BOLD='\033[1m'      NC='\033[0m'

info()  { echo -e "${CYAN}[info]${NC}  $*"; }
ok()    { echo -e "${GREEN}[ok]${NC}    $*"; }
warn()  { echo -e "${YELLOW}[warn]${NC}  $*"; }
err()   { echo -e "${RED}[error]${NC} $*"; }
step()  { echo -e "\n${BOLD}━━━ $* ━━━${NC}"; }

prompt() {
    local var_name="$1" prompt_text="$2" secret="${3:-false}" value=""
    while [[ -z "$value" ]]; do
        if [[ "$secret" == "true" ]]; then
            read -rsp "$prompt_text: " value; echo
        else
            read -rp "$prompt_text: " value
        fi
    done
    printf -v "$var_name" '%s' "$value"
}

prompt_default() {
    local var_name="$1" prompt_text="$2" default="$3" value=""
    read -rp "$prompt_text [$default]: " value
    printf -v "$var_name" '%s' "${value:-$default}"
}

confirm() {
    local reply=""
    read -rp "$1 [y/N]: " reply
    [[ "$reply" =~ ^[Yy] ]]
}

# ─── Export mode (early exit) ─────────────────────────────────────────

if [[ -n "$EXPORT_FILE" ]]; then
    MEDIA_SERVER_DIR="${HOME}/media-server"
    EXPORT_SCRIPT="$MEDIA_SERVER_DIR/scripts/setup/export-config.py"
    if [[ ! -f "$EXPORT_SCRIPT" ]]; then
        err "export-config.py not found. Is SlothServ installed?"
        exit 1
    fi
    python3 "$EXPORT_SCRIPT" -o "$EXPORT_FILE"
    exit 0
fi

# ─── Import mode (load saved config) ─────────────────────────────────

IMPORTED=false
if [[ -n "$IMPORT_FILE" ]]; then
    if [[ ! -f "$IMPORT_FILE" ]]; then
        err "Import file not found: $IMPORT_FILE"; exit 1
    fi
    IMPORTED=true
    info "Loading config from $IMPORT_FILE"

    TZ=$(python3 -c "import json; d=json.load(open('$IMPORT_FILE')); print(d['general']['timezone'])")
    NZBDAV_API_KEY=$(python3 -c "import json; d=json.load(open('$IMPORT_FILE')); print(d['nzbdav']['api_key'])")
    NZBDAV_WEBDAV_PASS_OBSCURED=$(python3 -c "import json; d=json.load(open('$IMPORT_FILE')); print(d['nzbdav']['webdav_password_obscured'])")
    INDEXER_NAME=$(python3 -c "import json; d=json.load(open('$IMPORT_FILE')); print(d['indexer']['name'])")
    INDEXER_URL=$(python3 -c "import json; d=json.load(open('$IMPORT_FILE')); print(d['indexer']['url'])")
    INDEXER_API_KEY=$(python3 -c "import json; d=json.load(open('$IMPORT_FILE')); print(d['indexer']['api_key'])")
    INDEXER_TV_CATS=$(python3 -c "import json; d=json.load(open('$IMPORT_FILE')); print(d['indexer'].get('tv_categories','5030,5040'))")
    INDEXER_ANIME_CATS=$(python3 -c "import json; d=json.load(open('$IMPORT_FILE')); print(d['indexer'].get('anime_categories','5070'))")
    INDEXER_MOVIE_CATS=$(python3 -c "import json; d=json.load(open('$IMPORT_FILE')); print(d['indexer'].get('movie_categories','2000,2010,2020,2030,2040,2045,2050,2060'))")
    USE_VPN=$(python3 -c "import json; d=json.load(open('$IMPORT_FILE')); print('true' if d['vpn']['enabled'] else 'false')")

    ok "Loaded: indexer=$INDEXER_NAME, vpn=$USE_VPN, tz=$TZ"
fi

# ─── 1. Preflight ────────────────────────────────────────────────────

step "1/8  Preflight"

if [[ "$(uname)" != "Darwin" ]]; then err "macOS only."; exit 1; fi
[[ "$(uname -m)" != "arm64" ]] && warn "Optimized for Apple Silicon — continuing anyway"
ok "macOS detected"

# ─── 2. Prerequisites ────────────────────────────────────────────────

step "2/8  Prerequisites"

if ! command -v brew &>/dev/null; then
    info "Installing Homebrew..."
    /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
    eval "$(/opt/homebrew/bin/brew shellenv)"
else ok "Homebrew"; fi

for pkg in colima docker docker-compose; do
    if ! brew list "$pkg" &>/dev/null; then
        info "Installing $pkg..."; brew install "$pkg"
    else ok "$pkg"; fi
done

command -v sqlite3 &>/dev/null && ok "sqlite3" || { err "sqlite3 not found"; exit 1; }

# ─── 3. Configuration ────────────────────────────────────────────────

step "3/8  Configuration"

MEDIA_SERVER_DIR="${HOME}/media-server"
PUID="$(id -u)"
PGID="$(id -g)"

if $IMPORTED; then
    prompt_default MEDIA_SERVER_DIR "Install directory" "$MEDIA_SERVER_DIR"

    echo ""
    echo -e "${BOLD}Plex${NC}  (claim tokens are one-time — you always need a fresh one)"
    echo "  Get one at https://plex.tv/claim (expires in 4 min)"
    prompt PLEX_CLAIM "Plex claim token"

    VPN_SOURCE="imported"
else
    echo ""
    echo -e "${BOLD}General${NC}"
    prompt_default MEDIA_SERVER_DIR  "Install directory"  "$MEDIA_SERVER_DIR"
    prompt_default TZ                "Timezone"            "America/New_York"

    echo ""
    echo -e "${BOLD}Plex${NC}"
    echo "  Get a claim token at https://plex.tv/claim (expires in 4 min)"
    prompt PLEX_CLAIM "Plex claim token"

    echo ""
    echo -e "${BOLD}Download client (NzbDAV)${NC}"
    echo "  NzbDAV is configured via its web UI after first boot."
    echo "  Choose a WebDAV password now — you'll enter it in the UI later."
    prompt NZBDAV_WEBDAV_PASS "WebDAV password (choose one)" true

    echo ""
    echo -e "${BOLD}Usenet indexer${NC}"
    echo "  Any Newznab-compatible indexer works (NzbGEEK, NZBFinder, DrunkenSlug, etc.)"
    prompt         INDEXER_NAME      "Indexer name (e.g. NzbGEEK)"
    prompt         INDEXER_URL       "Indexer Newznab URL (e.g. https://api.nzbgeek.info/)"
    prompt         INDEXER_API_KEY   "Indexer API key" true
    prompt_default INDEXER_TV_CATS   "TV categories (comma-separated Newznab IDs)"      "5030,5040"
    prompt_default INDEXER_ANIME_CATS "Anime categories (blank to skip)"                "5070"
    prompt_default INDEXER_MOVIE_CATS "Movie categories"                                "2000,2010,2020,2030,2040,2045,2050,2060"

    echo ""
    echo -e "${BOLD}VPN${NC}  (routes Usenet traffic through a WireGuard tunnel)"
    USE_VPN=false
    if confirm "Set up a VPN?"; then
        USE_VPN=true
        MOZVPN="/Applications/Mozilla VPN.app/Contents/MacOS/Mozilla VPN"
        HAS_MOZVPN=false
        [[ -x "$MOZVPN" ]] && HAS_MOZVPN=true

        if $HAS_MOZVPN && confirm "  Mozilla VPN detected — auto-generate server pool?"; then
            VPN_SOURCE="mozilla"
        else
            VPN_SOURCE="manual"
            echo "  Provide a standard WireGuard config ([Interface] + [Peer])."
            prompt WG_CONF_PATH "  Path to wg0.conf"
        fi
    fi
fi

# ─── 4. Files ─────────────────────────────────────────────────────────

step "4/8  Directory structure & config files"

REPO_URL="https://github.com/carterhudson/slothserv.git"
if [[ -d "$MEDIA_SERVER_DIR/.git" ]]; then
    info "Repo already cloned — pulling latest"
    git -C "$MEDIA_SERVER_DIR" pull --ff-only 2>/dev/null || true
else
    if [[ -d "$MEDIA_SERVER_DIR" ]] && [[ "$(ls -A "$MEDIA_SERVER_DIR" 2>/dev/null)" ]]; then
        info "Directory exists — cloning repo into temp and copying scripts"
        TMPCLONE=$(mktemp -d)
        git clone --depth 1 "$REPO_URL" "$TMPCLONE" 2>/dev/null
        cp -R "$TMPCLONE/scripts" "$MEDIA_SERVER_DIR/"
        cp "$TMPCLONE/bootstrap.sh" "$MEDIA_SERVER_DIR/" 2>/dev/null || true
        rm -rf "$TMPCLONE"
    else
        info "Cloning SlothServ repo..."
        git clone --depth 1 "$REPO_URL" "$MEDIA_SERVER_DIR"
    fi
fi
ok "Repository"

mkdir -p "$MEDIA_SERVER_DIR"/{config/{plex,nzbdav,overseerr,radarr,api-keys,gluetun/wireguard},data/media/{tv,anime,movies},mnt,logs}
chmod 700 "$MEDIA_SERVER_DIR/config/api-keys"
ok "Directories"

DL_CLIENT_HOST="nzbdav"
$USE_VPN && DL_CLIENT_HOST="gluetun"

# .env
cat > "$MEDIA_SERVER_DIR/.env" << ENVEOF
PUID=${PUID}
PGID=${PGID}
TZ=${TZ}
MEDIA_ROOT=${MEDIA_SERVER_DIR}/data
PLEX_CLAIM=${PLEX_CLAIM}
NZBDAV_PORT=3000
SONARR_PORT=8989
RADARR_PORT=7878
OVERSEERR_PORT=5055
PLEX_PORT=32400
ENVEOF
ok ".env"

# docker-compose.yml
COMPOSE_FILE="$MEDIA_SERVER_DIR/docker-compose.yml"
python3 - "$USE_VPN" "$COMPOSE_FILE" << 'PYCOMPOSE'
import sys

use_vpn = sys.argv[1] == "true"
out_path = sys.argv[2]

plex = """  plex:
    image: lscr.io/linuxserver/plex:latest
    container_name: plex
    environment:
      - PUID=${PUID}
      - PGID=${PGID}
      - TZ=${TZ}
      - PLEX_CLAIM=${PLEX_CLAIM}
      - VERSION=docker
    volumes:
      - ./config/plex:/config
      - ${MEDIA_ROOT}/media/tv:/tv
      - ${MEDIA_ROOT}/media/movies:/movies
      - ${MEDIA_ROOT}/media/anime:/anime
      - ./mnt:/mnt:rshared
    ports:
      - ${PLEX_PORT}:32400
    restart: unless-stopped
    depends_on:
      rclone:
        condition: service_started
"""

gluetun = """  gluetun:
    image: qmcgaw/gluetun
    container_name: gluetun
    cap_add:
      - NET_ADMIN
    environment:
      - VPN_SERVICE_PROVIDER=custom
      - VPN_TYPE=wireguard
      - HEALTH_VPN_DURATION_INITIAL=10s
      - HEALTH_VPN_DURATION_ADDITION=5s
      - HEALTH_TARGET_ADDRESS=1.1.1.1:443
    healthcheck:
      test: /gluetun-entrypoint healthcheck
      interval: 30s
      retries: 3
      start_period: 15s
      timeout: 5s
    volumes:
      - ./config/gluetun/wireguard:/gluetun/wireguard
    ports:
      - ${NZBDAV_PORT}:3000
    restart: unless-stopped
"""

nzbdav_vpn = """  nzbdav:
    image: nzbdav/nzbdav:alpha
    container_name: nzbdav
    network_mode: "service:gluetun"
    healthcheck:
      test: curl -f http://localhost:3000/health || exit 1
      interval: 1m
      retries: 3
      start_period: 5s
      timeout: 5s
    environment:
      - PUID=${PUID}
      - PGID=${PGID}
    volumes:
      - ./config/nzbdav:/config
      - ./mnt:/mnt
      - ${MEDIA_ROOT}/media:/data/media
    restart: unless-stopped
    depends_on:
      gluetun:
        condition: service_healthy
"""

nzbdav_plain = """  nzbdav:
    image: nzbdav/nzbdav:alpha
    container_name: nzbdav
    healthcheck:
      test: curl -f http://localhost:3000/health || exit 1
      interval: 1m
      retries: 3
      start_period: 5s
      timeout: 5s
    environment:
      - PUID=${PUID}
      - PGID=${PGID}
    volumes:
      - ./config/nzbdav:/config
      - ./mnt:/mnt
      - ${MEDIA_ROOT}/media:/data/media
    ports:
      - ${NZBDAV_PORT}:3000
    restart: unless-stopped
"""

rest = """  rclone:
    image: rclone/rclone:latest
    container_name: rclone
    restart: unless-stopped
    environment:
      - PUID=${PUID}
      - PGID=${PGID}
      - TZ=${TZ}
    volumes:
      - ./mnt:/mnt:rshared
      - ./rclone.conf:/config/rclone/rclone.conf
    cap_add:
      - SYS_ADMIN
    security_opt:
      - apparmor:unconfined
    devices:
      - /dev/fuse:/dev/fuse:rwm
    depends_on:
      nzbdav:
        condition: service_healthy
        restart: true
    command: >
      mount nzbdav: /mnt/remote/nzbdav
        --contimeout=30s
        --uid=${PUID}
        --gid=${PGID}
        --allow-other
        --links
        --use-cookies
        --vfs-cache-mode=full
        --vfs-cache-max-size=20G
        --vfs-cache-max-age=24h
        --buffer-size=0M
        --vfs-read-ahead=512M
        --dir-cache-time=20s

  sonarr:
    image: lscr.io/linuxserver/sonarr:latest
    container_name: sonarr
    environment:
      - PUID=${PUID}
      - PGID=${PGID}
      - TZ=${TZ}
    volumes:
      - sonarr_config:/config
      - ${MEDIA_ROOT}:/data
      - ./mnt:/mnt:rshared
    ports:
      - ${SONARR_PORT}:8989
    restart: unless-stopped
    depends_on:
      rclone:
        condition: service_started

  radarr:
    image: lscr.io/linuxserver/radarr:latest
    container_name: radarr
    environment:
      - PUID=${PUID}
      - PGID=${PGID}
      - TZ=${TZ}
    volumes:
      - ./config/radarr:/config
      - ${MEDIA_ROOT}:/data
      - ./mnt:/mnt:rshared
    ports:
      - ${RADARR_PORT}:7878
    restart: unless-stopped
    depends_on:
      rclone:
        condition: service_started

  seerr:
    image: ghcr.io/seerr-team/seerr:latest
    container_name: seerr
    init: true
    environment:
      - TZ=${TZ}
    volumes:
      - ./config/overseerr:/app/config
    ports:
      - ${OVERSEERR_PORT}:5055
    restart: unless-stopped
    depends_on:
      - plex
      - sonarr
      - radarr

  caddy:
    image: caddy:2-alpine
    container_name: caddy
    restart: unless-stopped
    ports:
      - "80:80"
    volumes:
      - ./config/caddy/Caddyfile:/etc/caddy/Caddyfile:ro
      - caddy_data:/data
      - caddy_config:/config
"""

volumes_block = """
volumes:
  sonarr_config:
    external: true
    name: media-server_sonarr_config
  caddy_data:
  caddy_config:
"""

with open(out_path, "w") as f:
    f.write("services:\n")
    f.write(plex)
    if use_vpn:
        f.write("\n" + gluetun)
        f.write("\n" + nzbdav_vpn)
    else:
        f.write("\n" + nzbdav_plain)
    f.write("\n" + rest)
    f.write(volumes_block)
PYCOMPOSE
ok "docker-compose.yml"

# Caddyfile — reverse proxy so services are reachable by hostname
# (sonarr.home.arpa, etc.) instead of raw port numbers.
mkdir -p "$MEDIA_SERVER_DIR/config/caddy"
NZBDAV_BACKEND="nzbdav:3000"
$USE_VPN && NZBDAV_BACKEND="gluetun:3000"
cat > "$MEDIA_SERVER_DIR/config/caddy/Caddyfile" << CADDYEOF
{
    auto_https off
}

sonarr.home.arpa:80 {
    reverse_proxy sonarr:8989
}

radarr.home.arpa:80 {
    reverse_proxy radarr:7878
}

plex.home.arpa:80 {
    reverse_proxy plex:32400
}

seerr.home.arpa:80 {
    reverse_proxy seerr:5055
}

nzbdav.home.arpa:80 {
    reverse_proxy ${NZBDAV_BACKEND}
}
CADDYEOF
ok "Caddyfile"

# /etc/hosts — point the hostnames at localhost so the browser sends
# them to Caddy. Idempotent via a marker block.
if ! grep -qF "# BEGIN SlothServ" /etc/hosts; then
    info "Adding SlothServ hostnames to /etc/hosts (requires sudo)"
    sudo tee -a /etc/hosts > /dev/null << 'HOSTSEOF'

# BEGIN SlothServ
127.0.0.1 sonarr.home.arpa radarr.home.arpa plex.home.arpa seerr.home.arpa nzbdav.home.arpa
# END SlothServ
HOSTSEOF
    ok "/etc/hosts patched"
else
    ok "/etc/hosts already has SlothServ block"
fi

# rclone.conf
if $IMPORTED; then
    RCLONE_OBSCURED="$NZBDAV_WEBDAV_PASS_OBSCURED"
else
    RCLONE_OBSCURED=$(docker run --rm rclone/rclone obscure "$NZBDAV_WEBDAV_PASS" 2>/dev/null || echo "PLACEHOLDER")
fi
cat > "$MEDIA_SERVER_DIR/rclone.conf" << RCLONEEOF
[nzbdav]
type = webdav
url = http://${DL_CLIENT_HOST}:3000/
vendor = other
user = admin
pass = ${RCLONE_OBSCURED}
RCLONEEOF
ok "rclone.conf"

# ─── 5. Colima & VPN ─────────────────────────────────────────────────

step "5/8  Colima & VPN"

COLIMA_CONF="$HOME/.colima/default/colima.yaml"
if [[ ! -f "$COLIMA_CONF" ]]; then
    info "Initializing Colima..."
    colima start --vm-type vz --mount-type virtiofs --network-address 2>/dev/null || true
    colima stop 2>/dev/null || true
fi
if [[ -f "$COLIMA_CONF" ]]; then
    grep -q "portForwarder:" "$COLIMA_CONF" && \
        sed -i '' 's/portForwarder: .*/portForwarder: ssh/' "$COLIMA_CONF"
    ok "Colima configured"
else
    warn "Colima config not found — configure manually"
fi

WG_DIR="$MEDIA_SERVER_DIR/config/gluetun/wireguard"
if $USE_VPN; then
    if [[ "${VPN_SOURCE:-}" == "imported" ]]; then
        info "Restoring VPN configs from import..."
        python3 -c "
import json, pathlib
d = json.load(open('$IMPORT_FILE'))
wg_dir = pathlib.Path('$WG_DIR')
wg_dir.mkdir(parents=True, exist_ok=True)
count = 0
for name, content in d['vpn'].get('wireguard_configs', {}).items():
    (wg_dir / name).write_text(content + '\n')
    count += 1
print(count)
" | while read count; do ok "$count VPN config(s) restored"; done

    elif [[ "${VPN_SOURCE:-}" == "mozilla" ]]; then
        info "Generating VPN server pool..."

        # Let the user pick a country or default to US
        echo ""
        echo "  Available server regions (showing first 20):"
        "$MOZVPN" servers 2>&1 | grep "Country:" | head -20 | sed 's/^/    /'
        echo ""
        prompt_default VPN_COUNTRY "  Country code for server pool" "us"

        # Grab up to 5 servers from that country
        mapfile -t ALL_SERVERS < <("$MOZVPN" servers 2>&1 \
            | grep -A 999 "code: ${VPN_COUNTRY})" \
            | grep "Server:" \
            | head -5 \
            | awk '{print $NF}')

        generated=0
        for server in "${ALL_SERVERS[@]}"; do
            "$MOZVPN" select "$server" 2>/dev/null
            sleep 1
            raw=$("$MOZVPN" wgconf 2>/dev/null || true)
            if [[ -n "$raw" ]]; then
                echo "$raw" \
                    | sed 's/Address = \([^,]*\),.*/Address = \1/' \
                    | sed 's/AllowedIPs = .*/AllowedIPs = 0.0.0.0\/0/' \
                    > "$WG_DIR/${server}.conf"
                ((generated++))
            fi
        done

        if (( generated > 0 )); then
            cp "$WG_DIR/${ALL_SERVERS[0]}.conf" "$WG_DIR/wg0.conf"
            ok "$generated VPN server configs generated"
        else
            warn "No configs produced — run bootstrap again after logging into Mozilla VPN"
        fi

    elif [[ "${VPN_SOURCE:-}" == "manual" ]]; then
        if [[ -f "$WG_CONF_PATH" ]]; then
            cp "$WG_CONF_PATH" "$WG_DIR/wg0.conf"
            ok "WireGuard config installed"
        else
            err "File not found: $WG_CONF_PATH"
            warn "Place wg0.conf in $WG_DIR manually"
        fi
    fi
else
    info "No VPN — NzbDAV connects directly"
fi

# ─── 6. Start services ───────────────────────────────────────────────

step "6/8  Starting services"

if ! colima status &>/dev/null; then
    info "Starting Colima..."
    colima start --vm-type vz --mount-type virtiofs --network-address
fi
ok "Colima running"

cd "$MEDIA_SERVER_DIR"
info "Pulling images..."
docker compose pull

# Sonarr's config lives in a named volume (media-server_sonarr_config) so
# it's on Colima's native ext4 instead of virtiofs — avoids the WAL
# corruption that hits SQLite DBs on the host-mounted filesystem.
docker volume inspect media-server_sonarr_config &>/dev/null || \
    docker volume create media-server_sonarr_config >/dev/null

info "Starting containers..."
docker compose up -d

info "Waiting for services to initialize..."
for _ in $(seq 1 90); do
    docker exec sonarr test -f /config/config.xml &>/dev/null && \
    [[ -f "$MEDIA_SERVER_DIR/config/radarr/config.xml" ]] && break
    sleep 1
done
docker exec sonarr test -f /config/config.xml &>/dev/null || { err "Sonarr didn't start."; exit 1; }

VM_IP=$(colima ls --json 2>/dev/null | python3 -c "
import json, sys
for line in sys.stdin:
    info = json.loads(line)
    if info.get('status') == 'Running':
        a = info.get('address', '')
        if a: print(a); break
" 2>/dev/null || echo "")
[[ -z "$VM_IP" ]] && VM_IP="localhost"
info "API target: $VM_IP"

docker cp sonarr:/config/config.xml /tmp/sonarr-config.xml
SONARR_KEY=$(xmllint --xpath '//ApiKey/text()' /tmp/sonarr-config.xml 2>/dev/null)
rm -f /tmp/sonarr-config.xml
(umask 077 && printf '%s' "$SONARR_KEY" > "$MEDIA_SERVER_DIR/config/api-keys/sonarr.key")
RADARR_KEY=$(xmllint --xpath '//ApiKey/text()' "$MEDIA_SERVER_DIR/config/radarr/config.xml" 2>/dev/null)

for port in 8989 7878; do
    for _ in $(seq 1 30); do
        key="$SONARR_KEY"; [[ "$port" == "7878" ]] && key="$RADARR_KEY"
        curl -sf "http://${VM_IP}:${port}/api/v3/system/status" -H "X-Api-Key: $key" &>/dev/null && break
        sleep 2
    done
done
ok "Sonarr & Radarr APIs ready"

# ─── 7. Configure Sonarr & Radarr ────────────────────────────────────

step "7/8  Configuring Sonarr & Radarr"

if $IMPORTED; then
    info "Using NzbDAV API key from import file"
    echo ""
    echo "NzbDAV still needs Usenet provider setup via its web UI."
    echo "Open ${BOLD}http://localhost:3000${NC} and:"
    echo "  1. Set up your Usenet provider        (Settings > Usenet)"
    echo "  2. Verify WebDAV password              (Settings > WebDAV)"
    echo "  3. Set Rclone Mount Dir: /mnt/remote/nzbdav  (Settings > SABnzbd)"
    echo "  4. Set Repairs Library Dir: /data/media      (Settings > Repairs)"
    echo ""
    echo "Press Enter once NzbDAV is configured..."
    read -r
else
    echo ""
    echo "NzbDAV needs initial setup via its web UI before we can continue."
    echo "Open ${BOLD}http://localhost:3000${NC} and:"
    echo "  1. Set up your Usenet provider        (Settings > Usenet)"
    echo "  2. Set the WebDAV password             (Settings > WebDAV)"
    echo "  3. Set Rclone Mount Dir: /mnt/remote/nzbdav  (Settings > SABnzbd)"
    echo "  4. Set Repairs Library Dir: /data/media      (Settings > Repairs)"
    echo "  5. Copy the API key from the UI"
    echo ""
    prompt NZBDAV_API_KEY "NzbDAV API key" true
fi

CONFIGURE_PY="$MEDIA_SERVER_DIR/scripts/setup/configure.py"

python3 "$CONFIGURE_PY" \
    --base-dir "$MEDIA_SERVER_DIR" \
    --vm-ip "$VM_IP" \
    --dl-host "$DL_CLIENT_HOST" \
    --dl-api-key "$NZBDAV_API_KEY" \
    --indexer-name "$INDEXER_NAME" \
    --indexer-url "$INDEXER_URL" \
    --indexer-api-key "$INDEXER_API_KEY" \
    --indexer-tv-cats "$INDEXER_TV_CATS" \
    --indexer-anime-cats "$INDEXER_ANIME_CATS" \
    --indexer-movie-cats "$INDEXER_MOVIE_CATS"

ok "Sonarr & Radarr configured"

# ─── 8. Watchdog & shell aliases ─────────────────────────────────────

step "8/8  Watchdog daemon & shell aliases"

ok "watchdog package (from repo)"

mkdir -p "$HOME/Library/LaunchAgents"
PLIST_PATH="$HOME/Library/LaunchAgents/com.slothserv.watchdog.plist"
cat > "$PLIST_PATH" << PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.slothserv.watchdog</string>
    <key>ProgramArguments</key>
    <array>
        <string>/usr/bin/python3</string>
        <string>${MEDIA_SERVER_DIR}/scripts/watchdog</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>${MEDIA_SERVER_DIR}/logs/watchdog-stdout.log</string>
    <key>StandardErrorPath</key>
    <string>${MEDIA_SERVER_DIR}/logs/watchdog-stderr.log</string>
</dict>
</plist>
PLISTEOF
launchctl load "$PLIST_PATH" 2>/dev/null || true
ok "Watchdog daemon"

if ! grep -q "# SlothServ" "$HOME/.zshrc" 2>/dev/null; then
    cat >> "$HOME/.zshrc" << 'ALIASEOF'

# SlothServ media server shortcuts
alias mstatus="python3 ~/media-server/scripts/cli/status.py"
alias mstatus-json="python3 ~/media-server/scripts/cli/status.py --json"
alias msearch="python3 ~/media-server/scripts/cli/episode-search.py"
alias mretry="python3 ~/media-server/scripts/cli/retry-failed.py"
alias mlogs="tail -30 ~/media-server/logs/watchdog.log"
alias mrestart="cd ~/media-server && docker compose down && colima restart && docker compose up -d"
ALIASEOF
    ok "Shell aliases → ~/.zshrc"
else
    ok "Shell aliases already present"
fi

# ─── Done ─────────────────────────────────────────────────────────────

echo ""
echo -e "${GREEN}${BOLD}━━━ SlothServ is running! ━━━${NC}"
echo ""
echo "Hostnames (via Caddy reverse proxy, no port numbers):"
echo "  http://plex.home.arpa      http://sonarr.home.arpa"
echo "  http://radarr.home.arpa    http://seerr.home.arpa"
echo "  http://nzbdav.home.arpa"
echo ""
echo "Remaining manual steps:"
echo "  1. Claim Plex at http://plex.home.arpa/web (or http://localhost:32400/web)"
echo "  2. Create libraries: TV → /tv, Anime → /anime, Movies → /movies"
echo "  3. Set Anime library: audio=Japanese, subtitles=English (always on)"
echo "  4. In NzbDAV: configure Sonarr integration (host: http://sonarr:8989)"
echo "  5. In Sonarr & Radarr: Settings > Import Lists > Plex Watchlist"
echo "  6. Set customConnections in Plex Preferences.xml to your LAN IP"
$USE_VPN && echo -e "\n  VPN active — watchdog auto-rotates servers on failure."
echo ""
echo "Commands:  mstatus  mlogs  msearch  mretry  mrestart"
echo ""
echo "Backup your config for easy migration to another machine:"
echo "  bash bootstrap.sh --export slothserv-config.json"
echo "  bash bootstrap.sh --import slothserv-config.json"
echo ""
