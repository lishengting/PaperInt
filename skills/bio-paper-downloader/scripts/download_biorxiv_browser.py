#!/usr/bin/env python3
"""
Download PDFs from bioRxiv / medRxiv using a real Chrome browser.

Bypasses Cloudflare by launching a real (headed) Chrome instance with a
persistent user-data directory, then connecting to it via CDP. Cloudflare's
JavaScript challenge runs in the real browser and passes normally.

Also handles generic PDF URLs by letting Chrome display the PDF, then
clicking the PDF viewer's built-in download button to save it.

Usage:
    python3 download_biorxiv_browser.py "10.1101/2025.01.01.123456" -o ./papers
    python3 download_biorxiv_browser.py "https://www.biorxiv.org/content/10.1101/2025.01.01.123456"
    python3 download_biorxiv_browser.py "https://example.com/paper.pdf" -o ./papers

Requirements: google-chrome (or chromium), pip install playwright
"""

import argparse
import asyncio
import atexit
import base64
import json
import os
import random
import re
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import urlparse

import log_utils; log_utils.install()

try:
    from playwright_stealth import Stealth
    _STEALTH_AVAILABLE = True
except ImportError:
    _STEALTH_AVAILABLE = False

try:
    from captcha_solver import try_solve_captcha
    _CAPTCHA_AVAILABLE = True
except ImportError:
    _CAPTCHA_AVAILABLE = False

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _bezier_curve(start, end, steps=25):
    """Generate control points for a human-like mouse movement between two points."""
    cx1 = start[0] + (end[0] - start[0]) * random.uniform(0.2, 0.4) + random.randint(-20, 20)
    cy1 = start[1] + (end[1] - start[1]) * random.uniform(0.1, 0.3) + random.randint(-15, 15)
    cx2 = start[0] + (end[0] - start[0]) * random.uniform(0.6, 0.8) + random.randint(-20, 20)
    cy2 = start[1] + (end[1] - start[1]) * random.uniform(0.7, 0.9) + random.randint(-15, 15)
    points = []
    for i in range(steps + 1):
        t = i / steps
        x = (1-t)**3 * start[0] + 3*(1-t)**2*t * cx1 + 3*(1-t)*t**2 * cx2 + t**3 * end[0]
        y = (1-t)**3 * start[1] + 3*(1-t)**2*t * cy1 + 3*(1-t)*t**2 * cy2 + t**3 * end[1]
        points.append((x, y))
    return points


async def _human_mouse_move(page, target_x, target_y, steps=25):
    """Move mouse to target using bezier curve with random micro-delays."""
    start_x, start_y = 100 + random.randint(0, 500), 100 + random.randint(0, 300)
    curve = _bezier_curve((start_x, start_y), (target_x, target_y), steps)
    for x, y in curve:
        await page.mouse.move(x, y)
        await asyncio.sleep(random.uniform(0.002, 0.015))


async def _apply_enhanced_stealth(page):
    """Enhanced stealth: playwright-stealth + fingerprint randomization + human behavior."""
    # 1. Apply playwright_stealth patches if available
    if _STEALTH_AVAILABLE:
        await Stealth().apply_stealth_async(page)

    # 2. Randomize viewport to a common resolution
    resolutions = [(1920, 1080), (1680, 1050), (1440, 900), (1366, 768)]
    w, h = random.choice(resolutions)
    await page.set_viewport_size({"width": w, "height": h})

    # 3. Override navigator properties for consistency
    await page.add_init_script("""
        Object.defineProperty(navigator, 'webdriver', {get: () => false});
        Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
        Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
        const origQuery = window.navigator.permissions.query;
        window.navigator.permissions.query = (parameters) => (
            parameters.name === 'notifications' ?
            Promise.resolve({state: Notification.permission}) :
            origQuery(parameters)
        );
    """)

    print("  [aabots-stealth] Enhanced stealth applied (fingerprint randomization + viewport variation)",
          file=sys.stderr)


def _load_aabots_handoff(path):
    if not path:
        return None
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        if data.get('version') != 1 or data.get('mode') != 'session':
            print(f"  [browser] ignoring invalid AABots handoff: {path}", file=sys.stderr)
            return None
        return data
    except Exception as e:
        print(f"  [browser] could not load AABots handoff: {e}", file=sys.stderr)
        return None


def _valid_playwright_cookie(cookie):
    if not isinstance(cookie, dict) or not cookie.get('name') or cookie.get('value') is None:
        return None
    allowed = {'name', 'value', 'url', 'domain', 'path', 'expires', 'httpOnly', 'secure', 'sameSite'}
    clean = {k: v for k, v in cookie.items() if k in allowed and v is not None}
    clean['value'] = str(clean['value'])
    if 'url' not in clean:
        clean.setdefault('path', '/')
        if not clean.get('domain'):
            return None
    return clean


async def _apply_aabots_handoff(ctx, handoff, log_prefix):
    if not handoff:
        return None
    cookies = []
    for cookie in handoff.get('browser_cookies') or []:
        clean = _valid_playwright_cookie(cookie)
        if clean:
            cookies.append(clean)
    final_url = handoff.get('final_url') or handoff.get('source_url')
    print(f"{log_prefix} AABots handoff: method={handoff.get('method')} cookies={len(cookies)} final_url={final_url}", file=sys.stderr)
    if cookies:
        try:
            await ctx.add_cookies(cookies)
            print(f"{log_prefix} Injected AABots cookies: {','.join(c['name'] for c in cookies)}", file=sys.stderr)
        except Exception as e:
            print(f"{log_prefix} AABots cookie injection failed: {e}", file=sys.stderr)
    return final_url


def _pick_chrome():
    """Find an available Chrome/Chromium binary."""
    for name in ['google-chrome', 'google-chrome-stable', 'chromium', 'chromium-browser']:
        try:
            subprocess.run([name, '--version'], capture_output=True, check=True)
            return name
        except (FileNotFoundError, subprocess.CalledProcessError):
            continue
    return None


def _find_free_port():
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(('', 0))
        return s.getsockname()[1]


def extract_doi(url_or_doi):
    """Extract DOI and determine server (biorxiv / medrxiv)."""
    if '/' in url_or_doi and '://' not in url_or_doi:
        return url_or_doi.strip(), 'biorxiv'

    parsed = urlparse(url_or_doi)
    path = parsed.path
    host = parsed.netloc or ''

    server = 'medrxiv' if 'medrxiv' in host else 'biorxiv'

    for pat in [r'/content/(10\.\d+/[\w.\-]+?)\.full\.pdf$',
                r'/content/(10\.\d+/[\w.\-]+)$']:
        m = re.search(pat, path)
        if m:
            return m.group(1), server

    return None, server


# ---------------------------------------------------------------------------
# Browser launcher
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Virtual display (Xvfb) — for headed Chrome on headless servers
# ---------------------------------------------------------------------------

_XVFB_PROC = None
_XVFB_DISPLAY = None


def _xvfb_start():
    """Start an Xvfb virtual display, reusing the one found for this process.

    Scans for a free display number on first call, then reuses it.
    Registers atexit cleanup so Xvfb is killed when the process exits.
    """
    global _XVFB_PROC, _XVFB_DISPLAY
    if _XVFB_PROC is not None and _XVFB_PROC.poll() is None:
        return True

    if _XVFB_DISPLAY is None:
        import socket
        for d in range(99, 110):
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                sock.connect(f'/tmp/.X11-unix/X{d}')
                sock.close()
            except (socket.error, FileNotFoundError):
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
    """Stop the Xvfb virtual display started by this process."""
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


def _profile_dir_from_cmdline(cmdline):
    parts = [p for p in cmdline.split('\x00') if p]
    if len(parts) <= 1:
        parts = cmdline.split()
    for i, part in enumerate(parts):
        if part.startswith('--user-data-dir='):
            return os.path.abspath(os.path.expanduser(part.split('=', 1)[1]))
        if part == '--user-data-dir' and i + 1 < len(parts):
            return os.path.abspath(os.path.expanduser(parts[i + 1]))
    return None


def _same_profile_dir(cmdline, profile_dir):
    found = _profile_dir_from_cmdline(cmdline)
    if not found:
        return False
    target = os.path.abspath(os.path.expanduser(profile_dir))
    return found == target or os.path.realpath(found) == os.path.realpath(target)


def _process_exists(pid):
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except OSError:
        return False


def _pid_alive(pid):
    if not _process_exists(pid):
        return False
    try:
        with open(f'/proc/{pid}/status', 'r', encoding='utf-8', errors='replace') as f:
            for line in f:
                if line.startswith('State:'):
                    return not line.split(':', 1)[1].lstrip().startswith('Z')
    except (FileNotFoundError, PermissionError, OSError):
        pass
    return True


def _safe_getpgid(pid):
    try:
        return os.getpgid(pid)
    except (ProcessLookupError, PermissionError, OSError):
        return None


def _read_proc_ppid(pid):
    try:
        with open(f'/proc/{pid}/stat', 'r', encoding='utf-8', errors='replace') as f:
            rest = f.read().split(') ', 1)[1].split()
        return int(rest[1])
    except (FileNotFoundError, PermissionError, OSError, IndexError, ValueError):
        return None


def _read_proc_children(pid):
    children = set()
    try:
        task_dir = f'/proc/{pid}/task'
        for tid in os.listdir(task_dir):
            try:
                with open(f'{task_dir}/{tid}/children', 'r', encoding='utf-8') as f:
                    children.update(int(p) for p in f.read().split() if p.isdigit())
            except (FileNotFoundError, PermissionError, OSError, ValueError):
                continue
    except (FileNotFoundError, PermissionError, OSError):
        pass
    if children:
        return children
    try:
        for pid_str in os.listdir('/proc'):
            if pid_str.isdigit():
                child_pid = int(pid_str)
                if _read_proc_ppid(child_pid) == pid:
                    children.add(child_pid)
    except (FileNotFoundError, PermissionError, OSError):
        pass
    return children


def _list_descendant_pids(pid):
    seen = set()
    stack = list(_read_proc_children(pid))
    while stack:
        child = stack.pop()
        if child in seen:
            continue
        seen.add(child)
        stack.extend(p for p in _read_proc_children(child) if p not in seen)
    return seen


def _list_pids_in_pgids(pgids):
    pgids = {pgid for pgid in pgids if pgid is not None}
    pids = set()
    if not pgids:
        return pids
    try:
        pid_names = os.listdir('/proc')
    except (FileNotFoundError, PermissionError, OSError):
        return pids
    for pid_str in pid_names:
        if not pid_str.isdigit():
            continue
        pid = int(pid_str)
        if _safe_getpgid(pid) in pgids:
            pids.add(pid)
    return pids


def _expand_owned_process_roots(root_pids):
    current_pid = os.getpid()
    current_pgid = _safe_getpgid(current_pid)
    roots = {int(pid) for pid in root_pids if int(pid) != current_pid and _pid_alive(int(pid))}
    owned = set(roots)
    pgids = set()
    for pid in roots:
        owned.update(_list_descendant_pids(pid))
        pgid = _safe_getpgid(pid)
        if pgid is not None and pgid != current_pgid:
            pgids.add(pgid)
    owned.update(_list_pids_in_pgids(pgids))
    owned.discard(current_pid)
    if current_pgid is not None:
        owned = {pid for pid in owned if _safe_getpgid(pid) != current_pgid}
    return {pid for pid in owned if _pid_alive(pid)}


def _terminate_pids(pids, timeout=5):
    current_pid = os.getpid()
    current_pgid = _safe_getpgid(current_pid)
    live = {int(pid) for pid in pids if int(pid) != current_pid and _pid_alive(int(pid))}
    pgids = {pgid for pgid in (_safe_getpgid(pid) for pid in live)
             if pgid is not None and pgid != current_pgid}
    live.update(pid for pid in _list_pids_in_pgids(pgids)
                if pid != current_pid and _pid_alive(pid))
    if current_pgid is not None:
        live = {pid for pid in live if _safe_getpgid(pid) != current_pgid}
    for sig in (signal.SIGTERM, signal.SIGKILL):
        for pgid in sorted(pgids):
            try:
                os.killpg(pgid, sig)
            except (ProcessLookupError, PermissionError, OSError):
                pass
        for pid in sorted(live):
            if _safe_getpgid(pid) in pgids:
                continue
            try:
                os.kill(pid, sig)
            except (ProcessLookupError, PermissionError, OSError):
                pass
        deadline = time.time() + timeout
        while time.time() < deadline:
            if not any(_pid_alive(pid) for pid in live):
                return
            time.sleep(0.1)


def _kill_chrome_for_profile(profile_dir):
    root_pids = []
    try:
        for pid_str in os.listdir('/proc'):
            if not pid_str.isdigit():
                continue
            pid = int(pid_str)
            if pid == os.getpid():
                continue
            try:
                with open(f'/proc/{pid}/cmdline', 'rb') as f:
                    cmdline = f.read().decode('utf-8', errors='replace')
            except (FileNotFoundError, PermissionError):
                continue
            if _same_profile_dir(cmdline, profile_dir):
                root_pids.append(pid)
    except (FileNotFoundError, PermissionError):
        return
    _terminate_pids(_expand_owned_process_roots(root_pids))


def _remove_profile_singletons(profile_dir):
    for name in ('SingletonLock', 'SingletonSocket', 'SingletonCookie'):
        try:
            os.unlink(os.path.join(profile_dir, name))
        except FileNotFoundError:
            pass
        except OSError:
            pass


def _cleanup_orphan_chrome_and_xvfb():
    """Kill orphan Chrome/Xvfb processes left by dead parent processes.

    Checks all Chrome processes with our temp-profile pattern and Xvfb
    processes on display 99-110. If the parent process no longer exists,
    the browser/display is an orphan and we kill it to free resources.
    """
    try:
        for pid_str in os.listdir('/proc'):
            if not pid_str.isdigit():
                continue
            pid = int(pid_str)
            try:
                with open(f'/proc/{pid}/cmdline', 'rb') as f:
                    cmdline = f.read().decode('utf-8', errors='replace')
            except (FileNotFoundError, PermissionError):
                continue

            # Check if this is a Chrome with our temp profile or an Xvfb
            is_ours = ('paper_cli_chrome_' in cmdline or
                       'paper_cli_pub_chrome_' in cmdline or
                       'paper_cli_scholar_chrome' in cmdline or
                       'chrome_profile' in cmdline)
            is_xvfb = cmdline.startswith('Xvfb') and any(
                f':{d}' in cmdline for d in range(99, 111))

            if not is_ours and not is_xvfb:
                continue

            # Check if parent still exists
            try:
                with open(f'/proc/{pid}/stat', 'rb') as f:
                    stat = f.read().decode('utf-8', errors='replace')
                ppid = int(stat.split(') ')[1].split()[0])
                if ppid == 1:
                    # Reparented to init — definitely orphaned
                    pass
                elif ppid > 1:
                    os.kill(ppid, 0)
                    continue  # Parent exists, skip
            except (FileNotFoundError, PermissionError, OSError, ValueError):
                pass  # Parent gone, kill the orphan

            _terminate_pids(_expand_owned_process_roots({pid}), timeout=1)
    except (FileNotFoundError, PermissionError):
        pass  # /proc not available, skip cleanup


class ChromeInstance:
    """Manage a Chrome browser process with CDP enabled.

    When headless=False, launches a virtual X display (Xvfb) so headed
    Chrome can render without a physical screen. This allows full browser
    rendering (GPU, WebGL, canvas) that passes anti-bot checks on servers.
    """

    def __init__(self, chrome_bin='google-chrome', profile_dir=None, port=None,
                 headless=True, xvfb=True):
        self.chrome_bin = chrome_bin
        raw_profile = profile_dir or tempfile.mkdtemp(prefix='paper_cli_chrome_')
        self.profile_dir = os.path.abspath(os.path.expanduser(raw_profile))
        self.port = port or _find_free_port()
        self.process = None
        self.headless = headless
        self.xvfb = xvfb

    def start(self):
        os.makedirs(self.profile_dir, exist_ok=True)

        # Clean up orphan Chrome/Xvfb from previous crashed runs
        _cleanup_orphan_chrome_and_xvfb()

        subprocess.run(['pkill', '-f', f'remote-debugging-port={self.port}'],
                       capture_output=True)
        _kill_chrome_for_profile(self.profile_dir)
        _remove_profile_singletons(self.profile_dir)
        time.sleep(1)

        args = [
            self.chrome_bin,
            f'--remote-debugging-port={self.port}',
            f'--user-data-dir={self.profile_dir}',
            '--no-first-run',
            '--no-default-browser-check',
            '--no-sandbox',
            '--disable-gpu',
        ]
        env = os.environ.copy()

        if self.headless:
            args.insert(1, '--headless=new')
            # Hide automation flags from detection
            args.insert(2, '--disable-blink-features=AutomationControlled')
        else:
            # Headed mode: need a display for Chrome to render.
            if self.xvfb:
                if not _xvfb_start():
                    raise RuntimeError('Xvfb is required for headed Chrome on headless servers')
                env['DISPLAY'] = os.environ.get('DISPLAY', ':99')
            else:
                # Level 3: use system display (must be pre-set)
                if 'DISPLAY' not in env:
                    raise RuntimeError('No DISPLAY available for headed Chrome (level 3 requires a real display)')
            print(f"  [browser] Virtual display: {env.get('DISPLAY', 'system')}", file=sys.stderr)

        args.append('about:blank')

        self.process = subprocess.Popen(
            args, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            preexec_fn=os.setsid, env=env)

        time.sleep(2)
        if self.process.poll() is not None:
            stderr = self.process.stderr.read().decode('utf-8', errors='replace')
            raise RuntimeError(f'Chrome exited immediately (code {self.process.returncode}): {stderr}')
        print(f"  [browser] Chrome PID {self.process.pid} on port {self.port}",
              file=sys.stderr)

    @property
    def cdp_url(self):
        return f'http://127.0.0.1:{self.port}'

    def stop(self):
        if self.process:
            pgid = _safe_getpgid(self.process.pid)
            if pgid is not None:
                _terminate_pids(_list_pids_in_pgids({pgid}) | {self.process.pid})
            else:
                _terminate_pids({self.process.pid})
        _kill_chrome_for_profile(self.profile_dir)
        self.process = None


# ---------------------------------------------------------------------------
# Main download logic
# ---------------------------------------------------------------------------

async def _wait_cloudflare(page, timeout=60, captcha_enabled=False,
                           captcha_api_key='', log_prefix=''):
    """Wait for a Cloudflare JS challenge to resolve, watching for Turnstile widgets.

    Cloudflare first shows a spinning JS challenge ("Just a moment...").
    If that fails, a Turnstile checkbox widget appears in the DOM.
    This function polls for both — title change means JS challenge passed,
    and Turnstile detection means we should solve via 2Captcha.
    """
    if captcha_enabled and captcha_api_key and _CAPTCHA_AVAILABLE:
        print(f"{log_prefix} 2Captcha: watching for Turnstile widget (max {timeout}s)",
              file=sys.stderr)
    for _ in range(timeout // 2):
        await asyncio.sleep(2)
        try:
            title = await page.title()
            if 'moment' not in title.lower():
                return True
        except Exception:
            if 'cloudflare' not in page.url.lower():
                return True
        # Still on Cloudflare — check if Turnstile widget has appeared
        if captcha_enabled and captcha_api_key and _CAPTCHA_AVAILABLE:
            solved = await try_solve_captcha(page, captcha_enabled,
                                              api_key=captcha_api_key,
                                              log_prefix=log_prefix,
                                              quiet=True)
            if solved:
                print(f"{log_prefix} 2Captcha: Turnstile solved, waiting for redirect...",
                      file=sys.stderr)
    return False


async def _download_generic_pdf(page, url_or_doi, output_path, timeout, wait=10):
    """Download a PDF that Chrome displays with its built-in viewer.

    Navigates to the PDF URL, lets Chrome render it, then clicks the
    viewer's download button to trigger a download we can capture.

    Also tries JS fetch from the page context as a fallback for sites
    without anti-bot protection.
    """
    result = {'success': False, 'file_path': None, 'file_size': 0, 'message': ''}

    print(f"  [browser] Navigating to PDF...", file=sys.stderr)
    await page.goto(url_or_doi, wait_until='networkidle',
                    timeout=timeout * 1000)
    # Some servers (PMC) serve a PoW challenge page ("Preparing to download...")
    # that computes a proof-of-work in JS, sets a cookie, then redirects.
    # Since PoW is CPU-bound, networkidle fires before it completes.
    # Wait for the page to resolve (redirect, or content-type change).
    poll_interval = min(3, max(1, wait // 3))
    for _ in range(max(1, wait // poll_interval)):
        await asyncio.sleep(poll_interval)
        ct = await page.evaluate('() => document.contentType')
        if ct == 'application/pdf':
            break
        # Check if page redirected away from the interstitial
        title = await page.title()
        if 'preparing to download' not in title.lower():
            break
    else:
        # PoW may have completed - try a reload to trigger the redirect
        await page.reload(wait_until='networkidle', timeout=timeout * 1000)
        await asyncio.sleep(wait)

    final_url = page.url
    ct = await page.evaluate('() => document.contentType')
    print(f"  [browser] Content-Type: {ct}", file=sys.stderr)

    # Method 1: Click the PDF viewer's built-in download button.
    # Chrome's PDF viewer (chrome-extension://mhjfbmdgcfjbbpaeojofohoefgiehjai/)
    # has a download button we can trigger.
    try:
        print(f"  [browser] Trying viewer download button...", file=sys.stderr)
        async with page.expect_download(timeout=min(timeout, 30) * 1000) as dl_info:
            # The download button is #download in the PDF viewer toolbar.
            # It may be in shadow DOM; try multiple approaches.
            clicked = await page.evaluate('''() => {
                // Try direct ID first
                let btn = document.querySelector('#download');
                if (btn) { btn.click(); return true; }
                // Try shadow DOM in pdf-viewer element
                const viewer = document.querySelector('pdf-viewer');
                if (viewer && viewer.shadowRoot) {
                    btn = viewer.shadowRoot.querySelector('#download');
                    if (btn) { btn.click(); return true; }
                }
                // Try the embed element
                const embed = document.querySelector('embed');
                if (embed) {
                    // The embed might have its own download mechanism
                    embed.click();
                    return true;
                }
                return false;
            }''')
            if not clicked:
                print(f"  [browser] Download button not found", file=sys.stderr)
                raise Exception("Download button not found")

        download = await dl_info.value
        tmp_path = await download.path()
        if tmp_path and os.path.exists(tmp_path):
            pdf_bytes = open(tmp_path, 'rb').read()
            if pdf_bytes.startswith(b'%PDF'):
                output_path.parent.mkdir(parents=True, exist_ok=True)
                with open(output_path, 'wb') as f:
                    f.write(pdf_bytes)
                result['success'] = True
                result['file_path'] = str(output_path)
                result['file_size'] = len(pdf_bytes)
                result['message'] = f'OK: {len(pdf_bytes)} bytes'
            else:
                result['message'] = 'Downloaded file is not a valid PDF'
                result['file_size'] = len(pdf_bytes)
            try:
                os.unlink(tmp_path)
            except Exception:
                pass
            return result
    except Exception as e:
        msg = str(e)
        if 'Timeout' in msg:
            print(f"  [browser] Download button timed out", file=sys.stderr)
        elif 'not found' in msg:
            print(f"  [browser] Download button not available", file=sys.stderr)
        else:
            print(f"  [browser] Download button approach failed: {e}", file=sys.stderr)

    # Method 2: JS fetch from page context.
    # This works for sites without anti-bot (e.g., university pages).
    try:
        print(f"  [browser] Trying JS fetch from page context...", file=sys.stderr)
        js_result = await page.evaluate("""
            async () => {
                const r = await fetch(window.location.href,
                    {credentials: 'include'});
                if (!r.ok) return {error: 'HTTP ' + r.status};
                const blob = await r.blob();
                return new Promise(resolve => {
                    const reader = new FileReader();
                    reader.onloadend = () =>
                        resolve({data: reader.result.split(',')[1],
                                 size: blob.size});
                    reader.onerror = () =>
                        resolve({error: 'FileReader failed'});
                    reader.readAsDataURL(blob);
                });
            }
        """)
        if isinstance(js_result, dict) and 'error' in js_result:
            print(f"  [browser] JS fetch failed: {js_result['error']}",
                  file=sys.stderr)
        else:
            pdf_bytes = base64.b64decode(js_result['data'])
            if pdf_bytes.startswith(b'%PDF'):
                output_path.parent.mkdir(parents=True, exist_ok=True)
                with open(output_path, 'wb') as f:
                    f.write(pdf_bytes)
                result['success'] = True
                result['file_path'] = str(output_path)
                result['file_size'] = len(pdf_bytes)
                result['message'] = f'OK: {len(pdf_bytes)} bytes'
                return result
            print(f"  [browser] JS fetch got non-PDF data ({len(pdf_bytes)} bytes)",
                  file=sys.stderr)
            # Show page title for diagnosis
            try:
                text = pdf_bytes[:2000].decode('utf-8', errors='replace')
                import re as _re
                m = _re.search(r'<title>(.*?)</title>', text, _re.IGNORECASE)
                if m:
                    print(f"  [browser] Page title: {m.group(1)}", file=sys.stderr)
            except Exception:
                pass
    except Exception as e:
        print(f"  [browser] JS fetch error: {e}", file=sys.stderr)

    result['message'] = 'Unable to capture PDF data from browser'
    return result


async def _do_download_via_browser(url_or_doi, output_dir, chrome_bin, timeout,
                                     headless, profile_dir=None, wait=10, xvfb=True,
                                     captcha_enabled=False, captcha_api_key='',
                                     stealth_enabled=False, aabots_stealth=False,
                                     aabots_handoff=None):
    """
    Core download logic. Returns dict result.
    """
    from playwright.async_api import async_playwright

    result = {'success': False, 'file_path': None, 'file_size': 0, 'message': ''}
    mode = 'headless' if headless else 'headed'

    doi, server = extract_doi(url_or_doi)

    # --- Generic URL path (not a biorxiv/medrxiv DOI) ---
    if doi is None:
        parsed = urlparse(url_or_doi)
        if not parsed.scheme:
            result['message'] = f'Not a valid URL or DOI: {url_or_doi}'
            return result

        safe_name = url_or_doi.split('/')[-1].split('?')[0] or 'download'
        if not safe_name.endswith('.pdf'):
            safe_name += '.pdf'
        output_path = Path(output_dir) / safe_name

        print(f"  [browser:{mode}] Generic URL: {url_or_doi}", file=sys.stderr)
        chrome = ChromeInstance(chrome_bin=chrome_bin or _pick_chrome(),
                                headless=headless,
                                profile_dir=profile_dir,
                                xvfb=xvfb)

        # Suppress TargetClosedError from Playwright's internal waiters
        # that fire after the browser connection is closed.
        loop = asyncio.get_event_loop()
        old_handler = loop.get_exception_handler()

        def _ignore_target_closed(loop, context):
            exc = context.get('exception')
            if exc and type(exc).__name__ == 'TargetClosedError':
                return
            if old_handler:
                old_handler(loop, context)
            else:
                loop.default_exception_handler(context)

        loop.set_exception_handler(_ignore_target_closed)
        try:
            chrome.start()

            async with async_playwright() as p:
                browser = await p.chromium.connect_over_cdp(chrome.cdp_url)
                ctx = browser.contexts[0]
                handoff_final_url = await _apply_aabots_handoff(ctx, aabots_handoff,
                                                                f'  [browser:{mode}]')

                about_page = ctx.pages[0] if ctx.pages else await ctx.new_page()
                cdp_tmp = await ctx.new_cdp_session(about_page)
                await cdp_tmp.send('Page.setDownloadBehavior', {
                    'behavior': 'allow',
                    'downloadPath': str(output_path.parent.absolute()),
                    'eventsEnabled': True,
                })
                await cdp_tmp.detach()

                page = await ctx.new_page()
                if aabots_stealth:
                    await _apply_enhanced_stealth(page)
                elif _STEALTH_AVAILABLE and stealth_enabled:
                    await Stealth().apply_stealth_async(page)
                sub_result = await _download_generic_pdf(page, handoff_final_url or url_or_doi,
                                                         output_path, timeout,
                                                         wait=wait)
                await browser.close()
                return sub_result

        except ImportError:
            result['message'] = 'Playwright not installed'
            return result
        except Exception as e:
            result['message'] = f'Browser download error ({mode}): {e}'
            return result
        finally:
            chrome.stop()
            loop.set_exception_handler(old_handler)

    # --- bioRxiv / medRxiv path ---
    article_url = f"https://www.{server}.org/content/{doi}"
    pdf_url = f"https://www.{server}.org/content/{doi}.full.pdf"
    safe_name = doi.replace('/', '_').replace('.', '_') + '.pdf'
    output_path = Path(output_dir) / safe_name
    output_path.parent.mkdir(parents=True, exist_ok=True)

    print(f"  [browser:{mode}] DOI: {doi}  server: {server}", file=sys.stderr)
    print(f"  [browser:{mode}] PDF URL: {pdf_url}", file=sys.stderr)

    chrome = ChromeInstance(chrome_bin=chrome_bin or _pick_chrome(),
                            headless=headless,
                            profile_dir=profile_dir,
                            xvfb=xvfb)

    # Suppress TargetClosedError from Playwright's internal waiters
    loop = asyncio.get_event_loop()
    old_handler = loop.get_exception_handler()

    def _ignore_target_closed(loop, context):
        exc = context.get('exception')
        if exc and type(exc).__name__ == 'TargetClosedError':
            return
        if old_handler:
            old_handler(loop, context)
        else:
            loop.default_exception_handler(context)

    loop.set_exception_handler(_ignore_target_closed)
    try:
        chrome.start()

        async with async_playwright() as p:
            print(f"  [browser:{mode}] Connecting to {chrome.cdp_url}", file=sys.stderr)
            browser = await p.chromium.connect_over_cdp(chrome.cdp_url)
            ctx = browser.contexts[0]
            await _apply_aabots_handoff(ctx, aabots_handoff, f'  [browser:{mode}]')

            # Step 1 — navigate to homepage, pass Cloudflare challenge
            print(f"  [browser:{mode}] Passing Cloudflare...", file=sys.stderr)
            page = await ctx.new_page()
            if aabots_stealth:
                await _apply_enhanced_stealth(page)
            elif _STEALTH_AVAILABLE and stealth_enabled:
                await Stealth().apply_stealth_async(page)
            await page.goto(f'https://www.{server}.org/', wait_until='domcontentloaded',
                            timeout=timeout * 1000)

            # Headless rarely passes Cloudflare JS challenges — give it a short leash
            cf_timeout = 15 if headless else 60
            if not await _wait_cloudflare(page, cf_timeout,
                                           captcha_enabled=captcha_enabled,
                                           captcha_api_key=captcha_api_key,
                                           log_prefix=f'  [browser:{mode}]'):
                result['message'] = 'Cloudflare challenge did not resolve'
                return result
            print(f"  [browser:{mode}] Cloudflare passed", file=sys.stderr)

            # Step 2 — load article page
            print(f"  [browser:{mode}] Loading article page...", file=sys.stderr)
            await page.goto(article_url, wait_until='domcontentloaded',
                            timeout=timeout * 1000)
            await asyncio.sleep(wait)

            # Step 3 — load PDF page (may display inline or trigger download)
            print(f"  [browser:{mode}] Loading PDF page...", file=sys.stderr)

            # Set download capture path in case Chrome downloads instead of displaying
            about_page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            cdp_tmp = await ctx.new_cdp_session(about_page)
            await cdp_tmp.send('Page.setDownloadBehavior', {
                'behavior': 'allow',
                'downloadPath': str(output_path.parent.absolute()),
                'eventsEnabled': True,
            })
            await cdp_tmp.detach()

            pdf_page = await ctx.new_page()

            if aabots_stealth:
                await _apply_enhanced_stealth(pdf_page)
            elif _STEALTH_AVAILABLE and stealth_enabled:
                await Stealth().apply_stealth_async(pdf_page)

            # Try download capture first (in case server sends attachment)
            pdf_bytes = None
            try:
                async with pdf_page.expect_download(timeout=min(timeout, 30) * 1000) as dl:
                    await pdf_page.goto(pdf_url, wait_until='commit',
                                        timeout=timeout * 1000)
                download = await dl.value
                tmp_path = await download.path()
                if tmp_path and os.path.exists(tmp_path):
                    pdf_bytes = open(tmp_path, 'rb').read()
                    try:
                        os.unlink(tmp_path)
                    except Exception:
                        pass
            except Exception:
                # Page displayed inline (or other failure), navigate normally
                try:
                    await pdf_page.goto(pdf_url, wait_until='domcontentloaded',
                                        timeout=timeout * 1000)
                except Exception:
                    pass
                await asyncio.sleep(wait)

                # The PDF page may trigger its own Cloudflare challenge
                await _wait_cloudflare(pdf_page, 30)

            if not pdf_bytes:
                ct = await pdf_page.evaluate('() => document.contentType')
                if ct != 'application/pdf':
                    result['message'] = f'Expected PDF but got Content-Type: {ct}'
                    return result

                # Step 4 — fetch PDF bytes via in-page JS (for inline display)
                print(f"  [browser:{mode}] Fetching PDF data via JS...", file=sys.stderr)
                js_result = await pdf_page.evaluate("""
                    async () => {
                        const r = await fetch(window.location.href, {credentials: 'include'});
                        if (!r.ok) return {error: 'HTTP ' + r.status};
                        const blob = await r.blob();
                        return new Promise(resolve => {
                            const reader = new FileReader();
                            reader.onloadend = () => resolve({data: reader.result.split(',')[1], size: blob.size});
                            reader.onerror = () => resolve({error: 'FileReader failed'});
                            reader.readAsDataURL(blob);
                        });
                    }
                """)

                if isinstance(js_result, dict) and 'error' in js_result:
                    result['message'] = f"JS fetch failed: {js_result['error']}"
                    return result

                pdf_bytes = base64.b64decode(js_result['data'])

            if len(pdf_bytes) < 10000:
                result['message'] = f'PDF too small ({len(pdf_bytes)} bytes)'
                result['file_size'] = len(pdf_bytes)
                return result

            with open(output_path, 'wb') as f:
                f.write(pdf_bytes)

            result['success'] = True
            result['file_path'] = str(output_path)
            result['file_size'] = len(pdf_bytes)
            result['message'] = f'OK ({mode}): {len(pdf_bytes)} bytes'
            return result

    except ImportError:
        result['message'] = (
            'Playwright not installed. Run: pip install playwright && playwright install chromium')
        return result
    except Exception as e:
        result['message'] = f'Browser download error ({mode}): {e}'
        return result
    finally:
        chrome.stop()
        loop.set_exception_handler(old_handler)


async def download_via_browser(url_or_doi, output_dir, chrome_bin=None, timeout=60,
                               fallback_level=2, wait=10, captcha_enabled=False,
                               captcha_api_key='', stealth_enabled=False,
                               aabots_stealth=False, aabots_handoff_path=None,
                               cookie_dir=None, headless_first=False):
    """
    Download a PDF from bioRxiv/medRxiv via a real Chrome browser, or any URL directly.

    fallback_level:
      0 — not applicable (should not be called without browser)
      1 — headless Chrome only (2 attempts)
      2 — Xvfb headed Chrome (default), headless if display unavailable
      3 — Xvfb headed → system display headed, headless if display unavailable

    With headless_first=True, try headless Chrome before headed fallbacks.

    Shares a single Chrome profile across retries so cookies persist.

    Returns dict: {success, file_path, file_size, message}
    """
    aabots_handoff = _load_aabots_handoff(aabots_handoff_path)
    profile_dir = cookie_dir or os.path.join(output_dir, 'chrome_profile')
    os.makedirs(profile_dir, exist_ok=True)
    _orig_display = os.environ.get('DISPLAY')
    result = {'success': False, 'file_path': None, 'file_size': 0, 'message': ''}

    def _is_chrome_fatal(msg):
        return 'ECONNREFUSED' in msg

    def _is_display_unavailable(msg):
        return 'Xvfb is required' in msg or 'No DISPLAY' in msg

    async def _try_headless(reason):
        nonlocal result
        print(f"  [browser] {reason}", file=sys.stderr)
        for attempt in range(2):
            if attempt > 0:
                print(f"  [browser] headless retry 2/2 after 5s...", file=sys.stderr)
                time.sleep(5)

            result = await _do_download_via_browser(url_or_doi, output_dir,
                                                    chrome_bin, timeout,
                                                    headless=True,
                                                    profile_dir=profile_dir,
                                                    wait=wait,
                                                    captcha_enabled=captcha_enabled,
                                                    captcha_api_key=captcha_api_key,
                                                    stealth_enabled=stealth_enabled,
                                                    aabots_stealth=aabots_stealth,
                                                    aabots_handoff=aabots_handoff)
            if result['success']:
                return 'success'
            msg = result.get('message', '')
            print(f"  [browser] headless failed: {msg}", file=sys.stderr)
            if _is_chrome_fatal(msg):
                return 'fatal'
            if 'Cloudflare' in msg:
                break
        return 'failed'

    if fallback_level < 1:
        result['message'] = 'Browser fallback disabled'
        return result

    headless_attempted = False
    display_unavailable = False

    if headless_first or fallback_level == 1:
        reason = 'trying headless Chrome first (--headless)...' if headless_first else 'trying headless Chrome (fallback level 1)...'
        status = await _try_headless(reason)
        headless_attempted = True
        if status in ('success', 'fatal') or fallback_level < 2:
            return result

    prefix = 'falling back to' if headless_first else 'trying'
    print(f"  [browser] {prefix} headed Chrome (Xvfb)...", file=sys.stderr)
    for attempt in range(3):
        if attempt > 0:
            print(f"  [browser] headed retry {attempt+1}/3 after 5s...", file=sys.stderr)
            time.sleep(5)

        result = await _do_download_via_browser(url_or_doi, output_dir,
                                                chrome_bin, timeout,
                                                headless=False,
                                                profile_dir=profile_dir,
                                                wait=wait, xvfb=True,
                                                captcha_enabled=captcha_enabled,
                                                captcha_api_key=captcha_api_key,
                                                stealth_enabled=stealth_enabled,
                                                aabots_stealth=aabots_stealth,
                                                aabots_handoff=aabots_handoff)
        if result['success']:
            return result
        msg = result.get('message', '')
        print(f"  [browser] headed (xvfb) failed: {msg}", file=sys.stderr)
        if _is_display_unavailable(msg):
            display_unavailable = True
            break
        if _is_chrome_fatal(msg):
            return result

    if fallback_level >= 3:
        if _orig_display:
            os.environ['DISPLAY'] = _orig_display
        else:
            os.environ.pop('DISPLAY', None)

        print(f"  [browser] falling back to headed Chrome (system display)...", file=sys.stderr)
        for attempt in range(3):
            if attempt > 0:
                print(f"  [browser] headed (system) retry {attempt+1}/3 after 5s...", file=sys.stderr)
                time.sleep(5)

            result = await _do_download_via_browser(url_or_doi, output_dir,
                                                    chrome_bin, timeout,
                                                    headless=False,
                                                    profile_dir=profile_dir,
                                                    wait=wait, xvfb=False,
                                                    captcha_enabled=captcha_enabled,
                                                    captcha_api_key=captcha_api_key,
                                                    stealth_enabled=stealth_enabled,
                                                    aabots_stealth=aabots_stealth,
                                                    aabots_handoff=aabots_handoff)
            if result['success']:
                return result
            msg = result.get('message', '')
            print(f"  [browser] headed (system) failed: {msg}", file=sys.stderr)
            if _is_display_unavailable(msg):
                display_unavailable = True
                break
            if _is_chrome_fatal(msg):
                return result

    if display_unavailable and not headless_attempted:
        status = await _try_headless('Xvfb/display unavailable; falling back to headless Chrome...')
        if status in ('success', 'fatal', 'failed'):
            return result

    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    p = argparse.ArgumentParser(
        description='Download bioRxiv / medRxiv PDF via real Chrome browser')
    p.add_argument('url_or_doi', help='bioRxiv/medRxiv URL or DOI, or any PDF URL')
    p.add_argument('-o', '--output-dir', default='.',
                   help='Output directory for PDF (default: .)')
    p.add_argument('--chrome-bin', default=None,
                   help='Path to Chrome binary (auto-detect if omitted)')
    p.add_argument('--timeout', type=int, default=60,
                   help='Page load timeout in seconds (default: 60)')
    p.add_argument('--wait', type=int, default=10,
                   help='Post-navigation wait in seconds (default: 10)')
    p.add_argument('--fallback-level', type=int, default=2, choices=[0, 1, 2, 3],
                   help='Browser fallback level (0=no-browser, 1=headless-only, 2=Xvfb headed default, 3=+system-display)')
    p.add_argument('--captcha', action='store_true', default=False,
                   help='Enable 2Captcha solving for Cloudflare challenges (default: off)')
    p.add_argument('--twocap-api', default='',
                   help='2Captcha API key (resolved from config.yaml download.twocaptcha_api_key_env)')
    p.add_argument('--stealth', action='store_true', default=False,
                   help='Enable playwright-stealth (default: off)')
    p.add_argument('--aabots-stealth', action='store_true', default=False,
                   help='Enable enhanced anti-bot stealth (fingerprint randomization + human behavior simulation)')
    p.add_argument('--aabots-handoff', default=None, help=argparse.SUPPRESS)
    p.add_argument('--cookie-dir', default=None,
                   help='Directory for persistent browser profile/cookies (default: <output-dir>/chrome_profile)')
    p.add_argument('--headless', action='store_true', default=False,
                   help='Try headless Chrome before headed fallbacks (default: headed/Xvfb first)')
    args = p.parse_args()

    result = asyncio.run(download_via_browser(
        args.url_or_doi, args.output_dir, args.chrome_bin, args.timeout,
        fallback_level=args.fallback_level, wait=args.wait,
        captcha_enabled=args.captcha,
        captcha_api_key=args.twocap_api,
        stealth_enabled=args.stealth,
        aabots_stealth=args.aabots_stealth,
        aabots_handoff_path=args.aabots_handoff,
        cookie_dir=args.cookie_dir,
        headless_first=args.headless))

    if result['success']:
        print(f"OK: {result['file_size']} bytes -> {result['file_path']}")
        sys.exit(0)
    else:
        print(f"FAILED: {result['message']}", file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    main()
