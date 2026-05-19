#!/usr/bin/env bash
set -euo pipefail

# PaperInt — installation script for Linux (Debian/Ubuntu, RHEL/CentOS/Fedora, Arch)
#
# Usage:
#   chmod +x install.sh
#   ./install.sh
#
# What this does:
#   1. Installs system packages (Chrome, poppler-utils, Xvfb, Python)
#   2. Creates Python virtual environment and installs Python dependencies
#   3. Installs Playwright Chromium browser
#   4. Creates required directories (data/, data/tmp/)

# --- Detect package manager --------------------------------------------------
if command -v apt-get &>/dev/null; then
    PKG_MGR="apt"
elif command -v dnf &>/dev/null; then
    PKG_MGR="dnf"
elif command -v yum &>/dev/null; then
    PKG_MGR="yum"
elif command -v pacman &>/dev/null; then
    PKG_MGR="pacman"
else
    echo "ERROR: Unsupported package manager. Install these manually:"
    echo "  - python3, python3-pip, python3-venv"
    echo "  - google-chrome or chromium"
    echo "  - poppler-utils (for pdftotext)"
    echo "  - xvfb or xorg-x11-server-Xvfb"
    echo "  - procps (for pgrep/pkill)"
    exit 1
fi

echo "==> Detected package manager: $PKG_MGR"

# --- System packages ---------------------------------------------------------
echo "==> Installing system packages..."

install_apt() {
    sudo apt-get update -qq
    sudo apt-get install -y -qq \
        python3 python3-pip python3-venv \
        poppler-utils \
        xvfb \
        procps
    # Chrome — try google-chrome-stable first, then chromium
    if ! command -v google-chrome &>/dev/null && ! command -v google-chrome-stable &>/dev/null; then
        echo "    Installing google-chrome..."
        if ! command -v wget &>/dev/null; then sudo apt-get install -y -qq wget; fi
        if ! command -v gpg &>/dev/null; then sudo apt-get install -y -qq gpg; fi
        wget -q -O /tmp/google-chrome.deb https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb
        sudo apt-get install -y -qq /tmp/google-chrome.deb 2>/dev/null || \
            sudo dpkg -i /tmp/google-chrome.deb && sudo apt-get install -f -y -qq
        rm -f /tmp/google-chrome.deb
    fi
}

install_dnf() {
    sudo dnf install -y -q \
        python3 python3-pip \
        poppler-utils \
        xorg-x11-server-Xvfb \
        procps-ng
    if ! command -v google-chrome &>/dev/null && ! command -v google-chrome-stable &>/dev/null; then
        echo "    Installing google-chrome..."
        sudo dnf install -y -q fedora-workstation-repositories 2>/dev/null || true
        sudo dnf config-manager --set-enabled google-chrome 2>/dev/null || true
        sudo dnf install -y -q google-chrome-stable 2>/dev/null || {
            wget -q -O /tmp/google-chrome.rpm https://dl.google.com/linux/direct/google-chrome-stable_current_x86_64.rpm
            sudo dnf install -y -q /tmp/google-chrome.rpm 2>/dev/null || \
                sudo rpm -i /tmp/google-chrome.rpm
            rm -f /tmp/google-chrome.rpm
        }
    fi
}

install_yum() {
    sudo yum install -y -q \
        python3 python3-pip \
        poppler-utils \
        xorg-x11-server-Xvfb \
        procps-ng
    if ! command -v google-chrome &>/dev/null && ! command -v google-chrome-stable &>/dev/null; then
        echo "    Installing google-chrome..."
        wget -q -O /tmp/google-chrome.rpm https://dl.google.com/linux/direct/google-chrome-stable_current_x86_64.rpm
        sudo yum install -y -q /tmp/google-chrome.rpm 2>/dev/null || \
            sudo rpm -i /tmp/google-chrome.rpm
        rm -f /tmp/google-chrome.rpm
    fi
}

install_pacman() {
    sudo pacman -S --noconfirm --quiet \
        python python-pip \
        poppler \
        xorg-server-xvfb \
        procps-ng
    if ! command -v google-chrome &>/dev/null && ! command -v google-chrome-stable &>/dev/null; then
        echo "    Installing chromium..."
        sudo pacman -S --noconfirm --quiet chromium 2>/dev/null || {
            echo "    WARNING: Could not install chromium. Install chrome/chromium manually for browser features."
        }
    fi
}

case "$PKG_MGR" in
    apt)   install_apt ;;
    dnf)   install_dnf ;;
    yum)   install_yum ;;
    pacman) install_pacman ;;
esac

echo "    System packages done."

# --- Python virtual environment ----------------------------------------------
PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_DIR="$PROJECT_DIR/venv"

if [ ! -d "$VENV_DIR" ]; then
    echo "==> Creating Python virtual environment at $VENV_DIR..."
    python3 -m venv "$VENV_DIR"
fi

echo "==> Installing Python packages..."
"$VENV_DIR/bin/pip" install --upgrade pip -q
"$VENV_DIR/bin/pip" install -r "$PROJECT_DIR/requirements.txt" -q

# --- Playwright Chromium -----------------------------------------------------
echo "==> Installing Playwright Chromium browser..."
"$VENV_DIR/bin/playwright" install --with-deps chromium

# --- Directories -------------------------------------------------------------
echo "==> Creating runtime directories..."
mkdir -p "$PROJECT_DIR/data" "$PROJECT_DIR/data/tmp"

# --- Verify ------------------------------------------------------------------
echo ""
echo "=============================================="
echo " Installation complete."
echo "=============================================="
echo ""
echo "Verified binaries:"
echo "  python:        $(command -v python3)"
echo "  pdftotext:     $(command -v pdftotext || echo 'NOT FOUND — install poppler-utils')"
echo "  chrome:        $(command -v google-chrome || command -v google-chrome-stable || command -v chromium || command -v chromium-browser || echo 'NOT FOUND')"
echo "  Xvfb:          $(command -v Xvfb || echo 'NOT FOUND — install xvfb')"
echo "  playwright:    $VENV_DIR/bin/playwright"
echo ""
echo "Required environment variable:"
echo "  export LLM_API_KEY=\"your-api-key\""
echo ""
echo "Optional:"
echo "  export TWOCAPTCHA_API_KEY=\"your-2captcha-key\"  # for CAPTCHA solving"
echo ""
echo "To activate the virtual environment:"
echo "  source $VENV_DIR/bin/activate"
echo ""
echo "Then run:"
echo "  python3 skills/bio-paper-search/scripts/paper_cli.py search -k \"CRISPR\" -n 3"
echo ""