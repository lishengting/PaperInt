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
import os
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
                       'paper_cli_scholar_chrome' in cmdline)
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

            try:
                os.kill(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
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
        self.profile_dir = profile_dir or tempfile.mkdtemp(prefix='paper_cli_chrome_')
        self.port = port or _find_free_port()
        self.process = None
        self.headless = headless
        self.xvfb = xvfb

    def start(self):
        os.makedirs(self.profile_dir, exist_ok=True)

        # Clean up orphan Chrome/Xvfb from previous crashed runs
        _cleanup_orphan_chrome_and_xvfb()

        # Kill any process already on our port
        subprocess.run(['pkill', '-f', f'remote-debugging-port={self.port}'],
                       capture_output=True)
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
            args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            preexec_fn=os.setsid, env=env)

        time.sleep(2)
        if self.process.poll() is not None:
            raise RuntimeError(f'Chrome exited immediately (code {self.process.returncode})')
        print(f"  [browser] Chrome PID {self.process.pid} on port {self.port}",
              file=sys.stderr)

    @property
    def cdp_url(self):
        return f'http://127.0.0.1:{self.port}'

    def stop(self):
        if self.process:
            try:
                os.killpg(os.getpgid(self.process.pid), signal.SIGTERM)
            except (ProcessLookupError, OSError):
                pass
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(os.getpgid(self.process.pid), signal.SIGKILL)


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
    for i in range(timeout // 2):
        await asyncio.sleep(2)
        try:
            title = await page.title()
            if 'moment' not in title.lower():
                return True
        except Exception:
            if 'cloudflare' not in page.url.lower():
                return True
        # Periodic debug: log page state every 10s
        if i % 5 == 0 and i > 0:
            try:
                n_iframes = await page.evaluate('() => document.querySelectorAll("iframe").length')
                print(f"{log_prefix} Cloudflare page state: title={title!r}, url={page.url[:100]!r}, iframes={n_iframes}",
                      file=sys.stderr)
            except Exception:
                pass
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
                                     captcha_enabled=False, captcha_api_key=''):
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

                about_page = ctx.pages[0] if ctx.pages else await ctx.new_page()
                cdp_tmp = await ctx.new_cdp_session(about_page)
                await cdp_tmp.send('Page.setDownloadBehavior', {
                    'behavior': 'allow',
                    'downloadPath': str(output_path.parent.absolute()),
                    'eventsEnabled': True,
                })
                await cdp_tmp.detach()

                page = await ctx.new_page()
                if _STEALTH_AVAILABLE:
                    await Stealth().apply_stealth_async(page)
                sub_result = await _download_generic_pdf(page, url_or_doi,
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

            # Step 1 — navigate to homepage, pass Cloudflare challenge
            print(f"  [browser:{mode}] Passing Cloudflare...", file=sys.stderr)
            page = await ctx.new_page()
            if _STEALTH_AVAILABLE:
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

            if _STEALTH_AVAILABLE:
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
                               captcha_api_key=''):
    """
    Download a PDF from bioRxiv/medRxiv via a real Chrome browser, or any URL directly.

    fallback_level:
      0 — not applicable (should not be called without browser)
      1 — headless Chrome only (2 attempts)
      2 — headless → Xvfb headed Chrome (default)
      3 — headless → Xvfb headed → system display headed

    Shares a single Chrome profile across retries so cookies persist.

    Returns dict: {success, file_path, file_size, message}
    """
    # Shared profile dir so retries share cookies (persists across downloads)
    profile_dir = os.path.join(output_dir, 'chrome_profile')
    os.makedirs(profile_dir, exist_ok=True)

    # Save original DISPLAY — _xvfb_start() overwrites it globally
    _orig_display = os.environ.get('DISPLAY')

    result = {'success': False, 'file_path': None, 'file_size': 0, 'message': ''}

    def _is_fatal(msg):
        """Chrome startup failures — retrying won't help."""
        return 'ECONNREFUSED' in msg or 'Xvfb is required' in msg or 'No DISPLAY' in msg

    # Try headless first (up to 2 attempts), break immediately on Cloudflare
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
                                                captcha_api_key=captcha_api_key)
        if result['success']:
            return result
        msg = result.get('message', '')
        print(f"  [browser] headless failed: {msg}", file=sys.stderr)
        if _is_fatal(msg):
            return result
        if 'Cloudflare' in msg:
            break

    if fallback_level < 2:
        return result

    # Fallback to Xvfb headed (3 attempts)
    print(f"  [browser] falling back to headed Chrome (Xvfb)...", file=sys.stderr)
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
                                                captcha_api_key=captcha_api_key)
        if result['success']:
            return result
        msg = result.get('message', '')
        print(f"  [browser] headed (xvfb) failed: {msg}", file=sys.stderr)
        if _is_fatal(msg):
            return result

    if fallback_level < 3:
        return result

    # Restore original DISPLAY before system display fallback.
    # _xvfb_start() polluted os.environ with its virtual display (:99).
    if _orig_display:
        os.environ['DISPLAY'] = _orig_display
    else:
        os.environ.pop('DISPLAY', None)

    # Fallback to system display headed (3 attempts)
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
                                                captcha_api_key=captcha_api_key)
        if result['success']:
            return result
        msg = result.get('message', '')
        print(f"  [browser] headed (system) failed: {msg}", file=sys.stderr)
        if _is_fatal(msg):
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
                   help='Browser fallback level (0=no-browser, 1=headless, 2=+xvfb, 3=+system-display)')
    p.add_argument('--captcha', action='store_true', default=False,
                   help='Enable 2Captcha solving for Cloudflare challenges (default: off)')
    p.add_argument('--twocap-api', default='',
                   help='2Captcha API key (resolved from config.yaml download.twocaptcha_api_key_env)')
    args = p.parse_args()

    result = asyncio.run(download_via_browser(
        args.url_or_doi, args.output_dir, args.chrome_bin, args.timeout,
        fallback_level=args.fallback_level, wait=args.wait,
        captcha_enabled=args.captcha,
        captcha_api_key=args.twocap_api))

    if result['success']:
        print(f"OK: {result['file_size']} bytes -> {result['file_path']}")
        sys.exit(0)
    else:
        print(f"FAILED: {result['message']}", file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    main()
