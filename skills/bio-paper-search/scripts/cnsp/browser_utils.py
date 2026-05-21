"""Shared browser utilities for CNSP and paper_cli.

Provides Xvfb virtual display management and Chrome CDP startup.
Importable from both paper_cli.py and cnsp/__init__.py without circular deps.
"""

from __future__ import annotations

import atexit
import os
import re
import signal
import socket
import subprocess
import sys
import tempfile
import time
from datetime import datetime


_XVFB_PROC = None
_XVFB_DISPLAY = None


def _ts():
    return datetime.now().strftime('[%H:%M:%S]')


# ---------------------------------------------------------------------------
# Xvfb virtual display helpers
# ---------------------------------------------------------------------------

def _force_xvfb_start():
    """Start Xvfb even if DISPLAY is already set (for Cloudflare bypass).

    Unlike _xvfb_start, this always creates a virtual display and does NOT
    reuse the system DISPLAY. Safe to call multiple times — reuses the
    already-running Xvfb process.
    """
    global _XVFB_PROC, _XVFB_DISPLAY
    if _XVFB_PROC is not None and _XVFB_PROC.poll() is None:
        os.environ['DISPLAY'] = _XVFB_DISPLAY
        return True

    if _XVFB_DISPLAY is None:
        for d in range(99, 110):
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                sock.connect(f'/tmp/.X11-unix/X{d}')
                sock.close()
            except (OSError, FileNotFoundError):
                _XVFB_DISPLAY = f':{d}'
                break
        if _XVFB_DISPLAY is None:
            _XVFB_DISPLAY = ':99'

    try:
        _XVFB_PROC = subprocess.Popen(
            ['Xvfb', _XVFB_DISPLAY, '-screen', '0', '1920x1080x24', '-ac'],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            preexec_fn=os.setsid)
        os.environ['DISPLAY'] = _XVFB_DISPLAY
        atexit.register(_xvfb_stop)
        time.sleep(0.5)
        return True
    except FileNotFoundError:
        return False


def _xvfb_start():
    """Start Xvfb virtual display if no DISPLAY is set."""
    global _XVFB_PROC, _XVFB_DISPLAY
    if _XVFB_PROC is not None and _XVFB_PROC.poll() is None:
        return True
    if os.environ.get('DISPLAY'):
        return True

    if _XVFB_DISPLAY is None:
        for d in range(99, 110):
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                sock.connect(f'/tmp/.X11-unix/X{d}')
                sock.close()
            except (OSError, FileNotFoundError):
                _XVFB_DISPLAY = f':{d}'
                break
        if _XVFB_DISPLAY is None:
            _XVFB_DISPLAY = ':99'

    try:
        _XVFB_PROC = subprocess.Popen(
            ['Xvfb', _XVFB_DISPLAY, '-screen', '0', '1920x1080x24', '-ac'],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            preexec_fn=os.setsid)
        os.environ['DISPLAY'] = _XVFB_DISPLAY
        atexit.register(_xvfb_stop)
        time.sleep(0.5)
        return True
    except FileNotFoundError:
        return False


def _xvfb_stop():
    """Stop Xvfb started by this process."""
    global _XVFB_PROC, _XVFB_DISPLAY
    if _XVFB_PROC is not None:
        try:
            os.killpg(os.getpgid(_XVFB_PROC.pid), signal.SIGTERM)
        except (ProcessLookupError, OSError):
            pass
        try:
            _XVFB_PROC.wait(timeout=3)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(_XVFB_PROC.pid), signal.SIGKILL)
            except Exception:
                pass
        _XVFB_PROC = None
        _XVFB_DISPLAY = None


# ---------------------------------------------------------------------------
# Chrome CDP startup
# ---------------------------------------------------------------------------

def _verify_chrome_port(port, timeout=5):
    """Poll Chrome's /json/version endpoint to confirm it is listening."""
    import urllib.request
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            urllib.request.urlopen(f'http://127.0.0.1:{port}/json/version', timeout=1)
            return True
        except Exception:
            time.sleep(0.5)
    return False


def _start_chrome_instance(headless):
    """Start a single Chrome instance with CDP on a random port.
    Returns port number on success, None on failure.
    """
    if headless:
        profile = os.path.join(tempfile.gettempdir(), 'paper_cli_cnsp_chrome')
    else:
        profile = os.path.join(tempfile.gettempdir(), 'paper_cli_scholar_chrome')
    os.makedirs(profile, exist_ok=True)

    # Reuse already-running Chrome with this profile
    r = subprocess.run(['pgrep', '-f', f'user-data-dir={profile}'],
                       capture_output=True, text=True)
    if r.returncode == 0 and r.stdout.strip():
        r2 = subprocess.run(['pgrep', '-a', '-f', f'user-data-dir={profile}'],
                            capture_output=True, text=True)
        m = re.search(r'--remote-debugging-port=(\d+)', r2.stdout)
        if m:
            port = int(m.group(1))
            if _verify_chrome_port(port):
                return port

    # Find a free port
    port = None
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(('', 0))
        port = s.getsockname()[1]

    # Kill anything already on that port
    subprocess.run(['pkill', '-f', f'remote-debugging-port={port}'],
                   capture_output=True)
    time.sleep(1)

    cmd = [
        'google-chrome',
        f'--remote-debugging-port={port}',
        f'--user-data-dir={profile}',
        '--no-first-run', '--no-default-browser-check', '--no-sandbox',
        '--disable-blink-features=AutomationControlled',
    ]
    if headless:
        cmd.append('--headless=new')
    cmd.append('about:blank')

    subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     preexec_fn=os.setsid)

    if _verify_chrome_port(port):
        return port
    return None


def start_chrome(force_headed=False):
    """Start Chrome with CDP enabled on a random port.

    If force_headed is False: tries headless first, then falls back to
    xvfb+headed Chrome. If force_headed is True: skips headless entirely
    and goes directly to xvfb+headed (for Cloudflare-protected sites).

    Returns the CDP port number. Raises RuntimeError when all modes fail.
    """
    if not force_headed:
        port = _start_chrome_instance(headless=True)
        if port:
            return port
        print(f"{_ts()}   Headless Chrome unavailable", file=sys.stderr)

    # When force_headed=True: always use Xvfb (avoid popping up GUI on desktop).
    # When NOT force_headed: use Xvfb only if DISPLAY is not already set.
    if force_headed:
        print(f"{_ts()}   Setting up Xvfb for Cloudflare-bypass headed Chrome...", file=sys.stderr)
        if not _force_xvfb_start():
            print(f"{_ts()}   Xvfb failed to start (xorg-x11-server-Xvfb not installed?)", file=sys.stderr)
            raise RuntimeError("Chrome failed: Xvfb could not start")
    elif not os.environ.get('DISPLAY'):
        print(f"{_ts()}   No DISPLAY set, starting Xvfb...", file=sys.stderr)
        if not _xvfb_start():
            print(f"{_ts()}   Xvfb failed to start (xorg-x11-server-Xvfb not installed?)", file=sys.stderr)
            raise RuntimeError("Chrome failed: Xvfb could not start")

    print(f"{_ts()}   Starting headed Chrome on DISPLAY={os.environ.get('DISPLAY')}...", file=sys.stderr)
    port = _start_chrome_instance(headless=False)
    if port:
        return port

    raise RuntimeError("Chrome failed: unable to start in either headless or headed mode")