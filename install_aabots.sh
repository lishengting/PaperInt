#!/usr/bin/env bash
set -euo pipefail

# PaperInt AABots supplemental installer
#
# Run install.sh first. This script only installs/checks the extra pieces used by
# --aabots, especially FlareSolverr and 2Captcha readiness.
#
# Usage:
#   chmod +x install_aabots.sh
#   ./install_aabots.sh                    # Python deps + checks only
#   ./install_aabots.sh --flaresolverr      # also start FlareSolverr container
#   ./install_aabots.sh --install-docker    # install Docker if missing, then start FlareSolverr
#   ./install_aabots.sh --check-only        # no installation, only checks

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_DIR="$PROJECT_DIR/venv"
CHECK_ONLY=0
INSTALL_FLARESOLVERR=0
INSTALL_DOCKER=0

usage() {
    sed -n '1,16p' "$0" | sed 's/^# \{0,1\}//'
}

while [ $# -gt 0 ]; do
    case "$1" in
        --flaresolverr)
            INSTALL_FLARESOLVERR=1
            ;;
        --install-docker)
            INSTALL_DOCKER=1
            INSTALL_FLARESOLVERR=1
            ;;
        --check-only)
            CHECK_ONLY=1
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "ERROR: unknown argument: $1" >&2
            usage >&2
            exit 1
            ;;
    esac
    shift
done

has_cmd() {
    command -v "$1" >/dev/null 2>&1
}

need_base_install() {
    if [ ! -x "$VENV_DIR/bin/python3" ]; then
        echo "ERROR: venv not found at $VENV_DIR" >&2
        echo "Run ./install.sh first, then re-run install_aabots.sh." >&2
        exit 1
    fi
}

install_python_deps() {
    if [ "$CHECK_ONLY" -eq 1 ]; then
        return
    fi

    echo "==> Installing/updating AABots Python dependencies in venv..."
    "$VENV_DIR/bin/pip" install -q \
        'cloudscraper>=1.2.71' \
        'curl_cffi>=0.5.10' \
        'playwright-stealth>=1.0.6' \
        '2captcha-python>=2.0.0'
}

check_python_deps() {
    echo "==> Checking AABots Python dependencies..."
    "$VENV_DIR/bin/python3" - <<'PY'
import importlib
checks = [
    ('cloudscraper', 'cloudscraper'),
    ('curl_cffi', 'curl_cffi'),
    ('playwright_stealth', 'playwright-stealth'),
    ('twocaptcha', '2captcha-python'),
]
missing = []
for module, package in checks:
    try:
        importlib.import_module(module)
        print(f'  OK: {package}')
    except ImportError:
        print(f'  MISSING: {package}')
        missing.append(package)
if missing:
    raise SystemExit(1)
PY
}

check_browser_deps() {
    echo "==> Checking browser dependencies from base install..."

    if has_cmd google-chrome; then
        echo "  OK: chrome ($(google-chrome --version))"
    elif has_cmd google-chrome-stable; then
        echo "  OK: chrome ($(google-chrome-stable --version))"
    elif has_cmd chromium; then
        echo "  OK: chromium ($(chromium --version))"
    elif has_cmd chromium-browser; then
        echo "  OK: chromium-browser ($(chromium-browser --version))"
    else
        echo "  MISSING: Chrome/Chromium. Run ./install.sh first."
    fi

    if has_cmd Xvfb; then
        echo "  OK: Xvfb ($(command -v Xvfb))"
    else
        echo "  MISSING: Xvfb. Run ./install.sh first."
    fi
}

install_docker_if_needed() {
    if has_cmd docker; then
        echo "==> Docker already installed: $(command -v docker)"
        return
    fi

    if [ "$CHECK_ONLY" -eq 1 ]; then
        echo "==> Docker not installed"
        return
    fi

    if [ "$INSTALL_DOCKER" -ne 1 ]; then
        echo "==> Docker not installed. Skipping Docker install."
        echo "    Re-run with --install-docker to install Docker and FlareSolverr."
        return
    fi

    echo "==> Installing Docker..."
    if has_cmd apt-get; then
        sudo apt-get update -qq
        sudo apt-get install -y -qq docker.io
        sudo systemctl enable --now docker || true
    elif has_cmd dnf; then
        sudo dnf install -y -q docker
        sudo systemctl enable --now docker || true
    elif has_cmd yum; then
        sudo yum install -y -q docker
        sudo systemctl enable --now docker || true
    elif has_cmd pacman; then
        sudo pacman -S --noconfirm --quiet docker
        sudo systemctl enable --now docker || true
    else
        echo "ERROR: unsupported package manager for Docker install." >&2
        echo "Install Docker manually, then re-run: ./install_aabots.sh --flaresolverr" >&2
        exit 1
    fi

    if getent group docker >/dev/null 2>&1; then
        sudo usermod -aG docker "$USER" || true
        echo "    Added $USER to docker group. You may need to log out/in or run: newgrp docker"
    fi
}

check_docker_access() {
    if ! has_cmd docker; then
        echo "  MISSING: docker"
        return 1
    fi

    if docker ps >/dev/null 2>&1; then
        echo "  OK: docker access"
        return 0
    fi

    echo "  WARNING: docker is installed but this user cannot access the Docker daemon."
    echo "           Try: sudo usermod -aG docker $USER && newgrp docker"
    echo "           If using snap Docker and /var/run/docker.sock is root:root, prefer docker.io or official Docker Engine."
    return 1
}

install_flaresolverr() {
    if [ "$INSTALL_FLARESOLVERR" -ne 1 ]; then
        return
    fi

    echo "==> Checking Docker access for FlareSolverr..."
    if ! check_docker_access; then
        echo "ERROR: cannot manage FlareSolverr without Docker access." >&2
        exit 1
    fi

    if docker ps -a --format '{{.Names}}' | grep -qx 'flaresolverr'; then
        if docker ps --format '{{.Names}}' | grep -qx 'flaresolverr'; then
            echo "==> FlareSolverr container already running."
        else
            echo "==> Starting existing FlareSolverr container..."
            docker start flaresolverr >/dev/null
        fi
    else
        echo "==> Creating FlareSolverr container..."
        docker run -d \
            --name=flaresolverr \
            -p 8191:8191 \
            -e LOG_LEVEL=info \
            --restart unless-stopped \
            ghcr.io/flaresolverr/flaresolverr:latest >/dev/null
    fi
}

check_flaresolverr_api() {
    echo "==> Checking FlareSolverr API..."
    if "$VENV_DIR/bin/python3" - <<'PY'
import json
import urllib.request
try:
    req = urllib.request.Request(
        'http://localhost:8191/v1',
        data=json.dumps({'cmd': 'sessions.list'}).encode(),
        headers={'Content-Type': 'application/json'},
    )
    with urllib.request.urlopen(req, timeout=5) as r:
        data = json.loads(r.read().decode())
    print(f"  OK: FlareSolverr {data.get('version', 'unknown')} ({data.get('status')})")
except Exception as e:
    print(f"  NOT READY: {type(e).__name__}: {e}")
    raise SystemExit(1)
PY
    then
        return 0
    fi
    return 1
}

check_twocaptcha() {
    echo "==> Checking 2Captcha configuration..."
    if [ -n "${TWOCAPTCHA_API_KEY:-}" ]; then
        echo "  OK: TWOCAPTCHA_API_KEY is set"
    else
        echo "  OPTIONAL: TWOCAPTCHA_API_KEY is not set; --aabots 2Captcha will be skipped."
    fi
}

need_base_install
install_python_deps
check_python_deps
check_browser_deps
install_docker_if_needed
install_flaresolverr
check_flaresolverr_api || true
check_twocaptcha

cat <<'EOF'

==============================================
 AABots supplemental setup complete.
==============================================

Examples:
  python3 skills/bio-paper-downloader/scripts/paper_cli.py --aabots Quick
  python3 skills/bio-paper-downloader/scripts/paper_cli.py --aabots Default,FlareSolverr
  python3 skills/bio-paper-downloader/scripts/paper_cli.py --aabots Full

Notes:
  - Default/Quick/CloudScraper/curl_cffi need no extra services.
  - FlareSolverr requires the local API at http://localhost:8191/v1.
  - 2Captcha requires TWOCAPTCHA_API_KEY.
EOF
